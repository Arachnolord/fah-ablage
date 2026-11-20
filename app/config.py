# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Pfade (aus der Umgebung) und die Einstellungen aus der Weboberfläche."""
import json
import os
import secrets
import threading
from pathlib import Path, PurePosixPath

DATA_DIR = Path(os.environ.get("DATA_DIR", "/config"))
EINGANG_DIR = Path(os.environ.get("EINGANG_DIR", "/eingang"))
ARCHIV_DIR = Path(os.environ.get("ARCHIV_DIR", "/archiv"))
# Wurzel für den Import (nur lesend): der persönliche Ordner auf dem NAS. Gewählt wird darunter in der Oberfläche.
QUELLEN_DIR = Path(os.environ.get("QUELLEN_DIR", "/quellen"))
KI_URL = os.environ.get("KI_URL", "http://fah-ablage-ki:8080")
# Hostnamen, unter denen die Oberfläche erreichbar sein darf (zusätzlich zu IP-Adressen und localhost).
# Schützt vor DNS-Rebinding: eine fremde Domain, die auf das NAS zeigt, wird abgewiesen.
ERLAUBTE_HOSTS = {h.strip().lower() for h in os.environ.get("ABLAGE_HOSTS", "").split(",") if h.strip()}

# Was der Betreiber in den Ordnern auf dem NAS sieht (nur zur Anzeige).
ANZEIGE_PFADE = {
    "eingang": os.environ.get("EINGANG_ANZEIGE", "/home/benutzer/Dokumente/Eingang"),
    "archiv": os.environ.get("ARCHIV_ANZEIGE", "/home/benutzer/Dokumente/Ablage"),
    "quellen": os.environ.get("QUELLEN_ANZEIGE", "/home/benutzer"),
}
# Bis 1.5 gab es nur den festen Altbestand-Ordner – er bleibt die Voreinstellung der Ordnerwahl.
ALTBESTAND_ANZEIGE = os.environ.get("IMPORT_ANZEIGE", "/home/benutzer/Dokumente/Privat")


def relativ_zur_quelle(anzeige: str) -> str | None:
    """Anzeigepfad auf dem NAS → Pfad relativ zur Quellen-Wurzel (None: liegt nicht darunter)."""
    try:
        rel = PurePosixPath(anzeige).relative_to(PurePosixPath(ANZEIGE_PFADE["quellen"]))
    except ValueError:
        return None
    return "" if str(rel) == "." else rel.as_posix()


STANDARD_IMPORT_ORDNER = relativ_zur_quelle(ALTBESTAND_ANZEIGE) or ""

DB_DATEI = DATA_DIR / "ablage.sqlite"
EINSTELLUNGEN_DATEI = DATA_DIR / "einstellungen.json"
USERS_FILE = DATA_DIR / "users.json"
SECRET_FILE = DATA_DIR / "secret.key"

STANDARD_ARTEN = [
    "Versicherungen", "Rente", "Steuer", "Bank & Finanzen", "Gesundheit", "Wohnen",
    "Energie & Nebenkosten", "Telefon & Internet", "Arbeit & Gehalt", "Fahrzeuge",
    "Behörden & Ausweise", "Familie & Schule", "Verträge & Abos", "Rechnungen & Belege",
    "Sonstiges",
]
STANDARD = {"arten": STANDARD_ARTEN, "unsicher_schwelle": 0.75, "art_hinweise": {},
            "import_ordner": STANDARD_IMPORT_ORDNER}

_lock = threading.Lock()


def load_json(pfad: Path, standard):
    try:
        return json.loads(Path(pfad).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return standard


def save_json(pfad: Path, daten) -> None:
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(pfad.suffix + ".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, pfad)


def lade_einstellungen() -> dict:
    with _lock:
        d = load_json(EINSTELLUNGEN_DATEI, {})
    e = {**STANDARD, **(d if isinstance(d, dict) else {})}
    arten = [a for a in e.get("arten", []) if isinstance(a, str) and a.strip()]
    if "Sonstiges" not in arten:
        arten.append("Sonstiges")
    e["arten"] = arten
    h = e.get("art_hinweise")
    e["art_hinweise"] = {str(k): str(v)[:200] for k, v in h.items() if isinstance(v, str) and v.strip()} \
        if isinstance(h, dict) else {}
    if not isinstance(e.get("import_ordner"), str):
        e["import_ordner"] = STANDARD_IMPORT_ORDNER
    try:
        e["unsicher_schwelle"] = min(0.99, max(0.0, float(e["unsicher_schwelle"])))
    except (TypeError, ValueError):
        e["unsicher_schwelle"] = STANDARD["unsicher_schwelle"]
    return e


def speichere_einstellungen(d: dict) -> None:
    with _lock:
        save_json(EINSTELLUNGEN_DATEI, d)


def get_secret() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        s = SECRET_FILE.read_text().strip()
        if s:
            return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    SECRET_FILE.write_text(s)
    os.chmod(SECRET_FILE, 0o600)
    return s
