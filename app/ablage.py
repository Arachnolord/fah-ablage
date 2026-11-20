# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Ablagepfade bilden und Dateien sicher (nie überschreibend) ablegen.

Pfad: <Art>/<Absender>/<JJJJ>/<JJJJ-MM-TT> <Titel>.<ext>
Zwischen Eingang, Arbeitsordner und Archiv liegen Mount-Grenzen, daher
shutil.move statt os.rename.
"""
import errno
import os
import re
import shutil
from datetime import date
from pathlib import Path, PurePosixPath

_VERBOTEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def saeubere(name: str, max_len: int = 80) -> str:
    s = _VERBOTEN.sub(" ", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip(". ")
    s = s[:max_len].rstrip(". ")
    return s or "Unbekannt"


def zielpfad(art: str, absender: str, datum: date, titel: str, endung: str) -> PurePosixPath:
    endung = re.sub(r"[^a-z0-9]", "", (endung or "").lower())[:8] or "bin"
    datei = f"{datum.isoformat()} {saeubere(titel, 60)}.{endung}"
    return PurePosixPath(saeubere(art, 40), saeubere(absender, 50), f"{datum.year:04d}", datei)


def _frei(archiv: Path, rel: PurePosixPath) -> PurePosixPath:
    kandidat = rel
    n = 2
    while (archiv / kandidat).exists():
        kandidat = rel.with_name(f"{rel.stem} ({n}){rel.suffix}")
        n += 1
    return kandidat


def ablegen(quelle: Path, rel: PurePosixPath, archiv: Path) -> PurePosixPath:
    """Verschiebt `quelle` ins Archiv; bei Namensgleichheit ` (n)`. Gibt den echten rel. Pfad zurück."""
    if ".." in rel.parts or rel.is_absolute():
        raise ValueError("ungültiger Ablagepfad")
    (archiv / rel).parent.mkdir(parents=True, exist_ok=True)
    ziel = _frei(archiv, rel)
    # Exklusiv reservieren, damit zwei gleichzeitige Ablagen sich nicht überschreiben.
    while True:
        try:
            fd = os.open(archiv / ziel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            os.close(fd)
            break
        except FileExistsError:
            ziel = _frei(archiv, ziel)
    try:
        _uebertragen(Path(quelle), archiv / ziel)
    except BaseException:
        try:
            if (archiv / ziel).stat().st_size == 0:
                (archiv / ziel).unlink()
        except OSError:
            pass
        raise
    return ziel


def _uebertragen(quelle: Path, ziel: Path) -> None:
    """Atomar auf den reservierten Namen: gleiche Platte → rename; sonst über eine
    versteckte .teil-Datei im Zielordner, fsync, os.replace – nie halbe Dateien unter echtem Namen."""
    try:
        os.rename(quelle, ziel)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    teil = ziel.with_name(f".{ziel.name}.teil")
    try:
        shutil.copyfile(quelle, teil)
        with open(teil, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(teil, ziel)
    except BaseException:
        teil.unlink(missing_ok=True)
        raise
    os.unlink(quelle)


def reste_entfernen(archiv: Path) -> int:
    """Nach einem Absturz liegengebliebene .teil-Dateien entfernen."""
    n = 0
    for p in Path(archiv).rglob(".*.teil"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def verschieben(alt_rel: PurePosixPath, neu_rel: PurePosixPath, archiv: Path) -> PurePosixPath:
    alt_rel = PurePosixPath(alt_rel)
    if PurePosixPath(neu_rel) == alt_rel:
        return alt_rel
    neu = ablegen(archiv / alt_rel, PurePosixPath(neu_rel), archiv)
    leere_ordner_entfernen((archiv / alt_rel).parent, archiv)
    return neu


def leere_ordner_entfernen(start: Path, archiv: Path) -> None:
    archiv = Path(archiv).resolve()
    p = Path(start).resolve()
    while p != archiv and archiv in p.parents:
        try:
            p.rmdir()
        except OSError:
            return
        p = p.parent
