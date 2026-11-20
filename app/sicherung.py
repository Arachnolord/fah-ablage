# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Sicherung und Wiederherstellung der ganzen Ablage in EINER Datei.

Inhalt (normales tar, mit jedem Packprogramm lesbar):
    manifest.json        Format, Version, Zahlen
    ablage.sqlite        Metadaten, Volltext, gelernte Regeln (konsistente Kopie)
    einstellungen.json   Arten-Liste, Schwelle
    archiv/…             alle abgelegten Dokumente in ihrer Ordnerstruktur
    papierkorb/…         Dokumente im Papierkorb
Zugang (Passwort) und Sitzungsschlüssel sind bewusst NICHT enthalten.
Wiederhergestellt wird nur in eine leere Ablage – nichts wird überschrieben.
"""
import io
import json
import os
import re
import sqlite3
import tarfile
import time
from pathlib import Path, PurePosixPath

from version import VERSION

FORMAT = 1
BEHALTEN = 3
_NAME = re.compile(r"^fah-ablage-sicherung-[0-9A-Za-z_\-() ]{1,60}\.tar$")
_META = {"manifest.json", "ablage.sqlite", "einstellungen.json"}


class SicherungFehler(RuntimeError):
    """Verständliche Meldung ohne Dokumentinhalte."""


def _versteckt(rel: PurePosixPath) -> bool:
    return any(t.startswith(".") for t in rel.parts)


def _bytes_hinzu(t: tarfile.TarFile, name: str, daten: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(daten), int(time.time()), 0o640
    t.addfile(info, io.BytesIO(daten))


def erstellen(db, archiv: Path, daten: Path, ziel: Path, einstellungen: dict, zeit: str | None = None) -> Path:
    archiv, daten, ziel = Path(archiv), Path(daten), Path(ziel)
    ziel.mkdir(parents=True, exist_ok=True)
    zeit = zeit or time.strftime("%Y%m%d-%H%M%S")
    name = f"fah-ablage-sicherung-{zeit}.tar"
    teil = ziel / f".{name}.teil"
    db_kopie = daten / f".sicherung-{os.getpid()}.sqlite"
    try:
        db.sicherungskopie(db_kopie)
        dateien = sorted(p for p in archiv.rglob("*")
                         if p.is_file() and not _versteckt(PurePosixPath(p.relative_to(archiv).as_posix())))
        korb = [Path(d["papierkorb_pfad"]) for d in db.papierkorb_alle() if d["papierkorb_pfad"]]
        manifest = {"format": FORMAT, "version": VERSION, "erstellt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "dokumente": db.anzahl_dokumente_alle(), "dateien": len(dateien)}
        with tarfile.open(teil, "w", format=tarfile.PAX_FORMAT) as t:
            _bytes_hinzu(t, "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
            t.add(db_kopie, "ablage.sqlite", recursive=False)
            _bytes_hinzu(t, "einstellungen.json", json.dumps(einstellungen or {}, ensure_ascii=False).encode())
            for p in dateien:
                t.add(p, "archiv/" + p.relative_to(archiv).as_posix(), recursive=False)
            for p in korb:
                if p.is_file():
                    t.add(p, "papierkorb/" + p.name, recursive=False)
        with open(teil, "rb+") as f:
            os.fsync(f.fileno())
        endgueltig = ziel / name
        os.replace(teil, endgueltig)
    finally:
        teil.unlink(missing_ok=True)
        db_kopie.unlink(missing_ok=True)
    aufraeumen(ziel)
    return endgueltig


def liste(ziel: Path) -> list[dict]:
    aus = []
    if Path(ziel).is_dir():
        for p in Path(ziel).iterdir():
            if p.is_file() and _NAME.match(p.name):
                st = p.stat()
                aus.append({"name": p.name, "groesse": st.st_size, "zeit": st.st_mtime})
    return sorted(aus, key=lambda e: e["name"], reverse=True)


def aufraeumen(ziel: Path, behalten: int = BEHALTEN) -> None:
    for e in liste(ziel)[behalten:]:
        (Path(ziel) / e["name"]).unlink(missing_ok=True)


def pfad_fuer(ziel: Path, name: str) -> Path | None:
    if not _NAME.match(name or ""):
        return None
    p = Path(ziel) / name
    if not p.is_file() or p.resolve().parent != Path(ziel).resolve():
        return None
    return p


def _plan(t: tarfile.TarFile) -> dict:
    """Alle Einträge prüfen, BEVOR irgendetwas geschrieben wird."""
    plan = {"archiv": [], "papierkorb": [], "meta": {}}
    for m in t.getmembers():
        roh = m.name
        rel = PurePosixPath(roh)
        if roh.startswith("/") or ".." in rel.parts or "\\" in roh:
            raise SicherungFehler("Die Sicherung enthält ungültige Pfade.")
        if m.isdir():
            continue
        if not m.isfile():
            raise SicherungFehler("Die Sicherung enthält Verknüpfungen oder Sonderdateien.")
        if roh in _META:
            plan["meta"][roh] = m
        elif rel.parts[0] == "archiv" and len(rel.parts) >= 2 and not _versteckt(PurePosixPath(*rel.parts[1:])):
            plan["archiv"].append((PurePosixPath(*rel.parts[1:]), m))
        elif rel.parts[0] == "papierkorb" and len(rel.parts) == 2 and not rel.name.startswith("."):
            plan["papierkorb"].append((rel.name, m))
        else:
            raise SicherungFehler("Die Sicherung enthält unerwartete Einträge.")
    if "manifest.json" not in plan["meta"] or "ablage.sqlite" not in plan["meta"]:
        raise SicherungFehler("Das ist keine Sicherung der FAH-Ablage.")
    return plan


def _schreiben(t: tarfile.TarFile, m: tarfile.TarInfo, ziel: Path, wurzel: Path) -> None:
    ziel = Path(ziel)
    if wurzel.resolve() not in ziel.resolve().parents:
        raise SicherungFehler("Die Sicherung enthält ungültige Pfade.")
    if ziel.exists():
        raise SicherungFehler("Im Archiv liegt schon eine Datei mit diesem Namen.")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    teil = ziel.with_name(f".{ziel.name}.teil")
    quelle = t.extractfile(m)
    with open(teil, "wb") as f:
        while block := quelle.read(1 << 20):
            f.write(block)
        f.flush()
        os.fsync(f.fileno())
    os.replace(teil, ziel)
    os.utime(ziel, (m.mtime, m.mtime))


def wiederherstellen(tar_pfad: Path, db, archiv: Path, daten: Path, einstellungen_speichern) -> dict:
    archiv, daten = Path(archiv), Path(daten)
    if not db.ist_leer() or any(p.is_file() and not p.name.startswith(".") for p in archiv.rglob("*")):
        raise SicherungFehler("Wiederherstellen geht nur in eine leere Ablage (frische Installation). "
                              "Es sind schon Dokumente vorhanden.")
    try:
        t = tarfile.open(tar_pfad, "r")
    except (tarfile.TarError, OSError):
        raise SicherungFehler("Die Datei ist keine lesbare Sicherung.") from None
    geschrieben: list[Path] = []
    db_tmp = daten / f".wiederherstellen-{os.getpid()}.sqlite"
    with t:
        plan = _plan(t)
        try:
            manifest = json.load(t.extractfile(plan["meta"]["manifest.json"]))
        except ValueError:
            raise SicherungFehler("Die Sicherung ist beschädigt (Inhaltsverzeichnis).") from None
        if manifest.get("format") != FORMAT:
            raise SicherungFehler("Diese Sicherung stammt aus einer nicht passenden Version.")
        try:
            with open(db_tmp, "wb") as f:
                f.write(t.extractfile(plan["meta"]["ablage.sqlite"]).read())
            try:
                c = sqlite3.connect(f"file:{db_tmp}?mode=ro", uri=True)
                ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                ok = ok and c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='dokumente'").fetchone()[0] == 1
                c.close()
            except sqlite3.DatabaseError:
                ok = False
            if not ok:
                raise SicherungFehler("Die Datenbank in der Sicherung ist beschädigt.")
            try:
                for rel, m in plan["archiv"]:
                    z = archiv / rel
                    _schreiben(t, m, z, archiv)
                    geschrieben.append(z)
                korb = daten / "papierkorb"
                korb.mkdir(parents=True, exist_ok=True)
                for name, m in plan["papierkorb"]:
                    z = korb / name
                    _schreiben(t, m, z, korb)
                    geschrieben.append(z)
                n = db.uebernehmen(db_tmp, korb)
            except BaseException:
                for p in geschrieben:          # halbe Wiederherstellung wieder entfernen
                    p.unlink(missing_ok=True)
                raise
            if "einstellungen.json" in plan["meta"]:
                try:
                    e = json.load(t.extractfile(plan["meta"]["einstellungen.json"]))
                    if isinstance(e, dict) and e:
                        einstellungen_speichern(e)
                except ValueError:
                    pass
        finally:
            for endung in ("", "-wal", "-shm", "-journal"):
                Path(str(db_tmp) + endung).unlink(missing_ok=True)
    return {"dokumente": n, "dateien": len(geschrieben)}
