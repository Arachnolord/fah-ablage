# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Ordnerwahl für den Import: beliebiger Ordner unterhalb der Quellen-Wurzel (nur lesend).

Eingang, Archiv und der Datenordner der App sind ausgenommen – samt allem darunter –,
damit nie abgelegte Dokumente oder Dateien in Arbeit ein zweites Mal eingelesen werden.
Erkannt werden sie zweifach: an der Datei-Identität (Gerät + Inode; Bind-Mounts zeigen
dieselbe) und am Anzeigepfad auf dem NAS (falls die Identität einmal nicht passt).
"""
import os
import time
from pathlib import Path, PurePosixPath

# Video und Musik bringen nichts in der Ablage und füllen nur das Archiv.
MEDIEN = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg", ".webm", ".3gp", ".mts",
          ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".wma", ".aiff", ".opus"}
_IGNORIEREN = (".part", ".tmp", ".crdownload", ".download", ".partial", ".teil")
_IGNORIER_NAMEN = {"thumbs.db", "desktop.ini", "icon\r"}
ZAEHL_GRENZE = 20000
ZAEHL_ZEIT = 2.0


def lesbar(name: str) -> bool:
    """Namen, die kein gültiges UTF-8 sind (alte Latin-1-Namen), kann weder SQLite noch JSON speichern."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def datei_ignorieren(name: str) -> bool:
    n = name.lower()
    return (not lesbar(name) or name.startswith((".", "~$")) or n.endswith(_IGNORIEREN)
            or n in _IGNORIER_NAMEN)


def ordner_ignorieren(name: str) -> bool:
    """Versteckte und System-Ordner (@eaDir, #recycle, $RECYCLE.BIN …) und unlesbare Namen."""
    return not lesbar(name) or name.startswith((".", "@", "#", "$", "~"))


def _darunter(pfad: str, oben: str) -> bool:
    try:
        PurePosixPath(pfad).relative_to(PurePosixPath(oben))
        return True
    except ValueError:
        return False


def normalisiere(rel: str) -> str | None:
    """Relativen Ordnerpfad säubern; None, wenn er nach oben oder ins Versteckte führt."""
    rel = (rel or "").strip().replace("\\", "/").strip("/")
    if not rel:
        return ""
    teile = [t for t in PurePosixPath(rel).parts if t not in ("", ".")]
    if any(t == ".." or ordner_ignorieren(t) for t in teile):
        return None
    return "/".join(teile)


def _identitaet(p: Path):
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


class Quellen:
    def __init__(self, wurzel, sperr_ordner=(), sperr_rel=(), anzeige: str = "", sperr_anzeige=()):
        self.wurzel = Path(wurzel)
        self.anzeige = (anzeige or str(self.wurzel)).rstrip("/") or "/"
        self._sperr_ordner = [Path(p) for p in sperr_ordner]
        self._sperr_rel = {r for r in (normalisiere(x) for x in sperr_rel) if r}
        # Liegt die Wurzel selbst in Eingang/Archiv (oder ist es), ist die ganze Ordnerwahl aus.
        self._aus = any(_darunter(self.anzeige, s) for s in sperr_anzeige if s)

    def _sperren(self) -> set:
        return {i for i in (_identitaet(p) for p in self._sperr_ordner) if i}

    def _gesperrt(self, p: Path, rel: str, sperren: set) -> bool:
        if rel in self._sperr_rel:
            return True
        return _identitaet(p) in sperren

    def aufloesen(self, rel: str) -> Path | None:
        """Ordner zum relativen Pfad – nur wenn er existiert, kein Link ist und nicht gesperrt ist."""
        rel = normalisiere(rel)
        if rel is None or self._aus or not self.wurzel.is_dir():
            return None
        sperren, p, bisher = self._sperren(), self.wurzel, []
        if _identitaet(self.wurzel) in sperren:
            return None
        for teil in (rel.split("/") if rel else []):
            p, bisher = p / teil, bisher + [teil]
            if p.is_symlink() or not p.is_dir() or self._gesperrt(p, "/".join(bisher), sperren):
                return None
        try:
            if not p.resolve().is_relative_to(self.wurzel.resolve()):
                return None
        except OSError:
            return None
        return p

    def anzeige_pfad(self, rel: str) -> str:
        rel = normalisiere(rel) or ""
        return f"{self.anzeige}/{rel}" if rel else self.anzeige

    def unterordner(self, rel: str) -> list[dict] | None:
        p = self.aufloesen(rel)
        if p is None:
            return None
        rel, sperren, aus = normalisiere(rel), self._sperren(), []
        try:
            eintraege = list(os.scandir(p))
        except OSError:
            return []
        for e in eintraege:
            try:
                if e.is_symlink() or not e.is_dir() or ordner_ignorieren(e.name):
                    continue
            except OSError:
                continue
            kind = f"{rel}/{e.name}" if rel else e.name
            if not self._gesperrt(Path(e.path), kind, sperren):
                aus.append({"name": e.name, "pfad": kind})
        return sorted(aus, key=lambda o: o["name"].casefold())

    def dateien(self, rel: str, mit_ordnern: bool = False):
        """(Datei, Unterpfad relativ zum gewählten Ordner) – sortiert, ohne Gesperrtes/Verstecktes/Medien.
        mit_ordnern: zusätzlich (None, Unterpfad) je besuchtem Ordner, damit Zählen auch in leeren Bäumen abbricht."""
        start = self.aufloesen(rel)
        if start is None:
            return
        rel, sperren = normalisiere(rel), self._sperren()
        for ordner, unter, namen in os.walk(start, followlinks=False):
            basis = Path(ordner)
            innen = basis.relative_to(start).as_posix()
            innen = "" if innen == "." else innen
            behalten = []
            for u in sorted(unter, key=str.casefold):
                kind_rel = "/".join(x for x in (rel, innen, u) if x)
                if ordner_ignorieren(u) or (basis / u).is_symlink() or self._gesperrt(basis / u, kind_rel, sperren):
                    continue
                behalten.append(u)
            unter[:] = behalten
            if mit_ordnern:
                yield None, innen
            for n in sorted(namen, key=str.casefold):
                p = basis / n
                if datei_ignorieren(n) or p.suffix.lower() in MEDIEN or p.is_symlink() or not p.is_file():
                    continue
                yield p, innen

    def darf_lesen(self, ordner: str, herkunft: str, pfad) -> bool:
        """Beim Verarbeiten erneut prüfen: Liegt die eingereihte Datei noch in einem erlaubten Ordner (keine Links,
        nichts Gesperrtes)? Zwischen Einreihen und Lesen können Tage liegen."""
        p = Path(pfad)
        eltern = self.aufloesen("/".join(x for x in (ordner, herkunft) if x))
        return eltern is not None and p.parent == eltern and lesbar(p.name) and not datei_ignorieren(p.name)

    def zaehlen(self, rel: str, grenze: int = ZAEHL_GRENZE, zeit: float = ZAEHL_ZEIT) -> tuple[int, bool]:
        """Wie viele Dateien der Import einlesen würde; (Anzahl, vollständig gezählt)."""
        ende, n = time.monotonic() + zeit, 0
        for p, _ in self.dateien(rel, mit_ordnern=True):
            if p is not None:
                n += 1
            if n >= grenze or time.monotonic() > ende:
                return n, False
        return n, True
