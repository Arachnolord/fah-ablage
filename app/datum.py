# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Briefdatum aus deutschem Text finden (Rückfall, wenn die KI keins liefert)."""
import re
from datetime import date, timedelta

_MONATE = {
    "jan": 1, "januar": 1, "jänner": 1, "feb": 2, "februar": 2, "mär": 3, "märz": 3, "maerz": 3,
    "mrz": 3, "apr": 4, "april": 4, "mai": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10,
    "nov": 11, "november": 11, "dez": 12, "dezember": 12,
}
_NUM = re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?!\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_TEXT = re.compile(r"(?<!\d)(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]{3,9})\.?\s+(\d{4})(?!\d)")
_GUT = re.compile(r"(?:^|[^a-zäöü])(datum|den)\s*:?\s*$", re.I)
_SCHLECHT = re.compile(r"(geburt|geb\.|fällig|faellig|gültig|gueltig|bis|ab\s*dem|zahlbar|vom|seit|beginn|ende)\W*$", re.I)
_ORT = re.compile(r"(?:^|\n)\s*[A-ZÄÖÜ][\wäöüß .\-/()]{1,40},\s*(?:den\s+)?$")


def _kandidaten(text: str):
    for m in _NUM.finditer(text):
        t, mo, j = int(m.group(1)), int(m.group(2)), m.group(3)
        j = int(j) + (2000 if len(j) == 2 and int(j) <= 69 else 1900 if len(j) == 2 else 0)
        yield m.start(), t, mo, j
    for m in _ISO.finditer(text):
        yield m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))
    for m in _TEXT.finditer(text):
        mo = _MONATE.get(m.group(2).lower())
        if mo:
            yield m.start(), int(m.group(1)), mo, int(m.group(3))


def finde_briefdatum(text: str, heute: date) -> date | None:
    text = text or ""
    beste, beste_wert = None, None
    for pos, t, mo, j in _kandidaten(text):
        try:
            d = date(j, mo, t)
        except ValueError:
            continue
        if d.year < 1950 or d > heute + timedelta(days=30):
            continue
        davor = text[max(0, pos - 40):pos]
        wert = -pos / 2000.0
        if _GUT.search(davor) and not re.search(r"geburts\w*\s*:?\s*$", davor, re.I):
            wert += 10
        if _ORT.search(text[max(0, pos - 60):pos]):
            wert += 8
        if _SCHLECHT.search(davor):
            wert -= 6
        if beste_wert is None or wert > beste_wert:
            beste, beste_wert = d, wert
    if beste_wert is not None and beste_wert < -3:
        return None
    return beste


# ── Abrechnungszeitraum (Lohn-/Gehaltszettel haben oft kein Briefdatum, nur „März 2019“ oder 01.03.–31.03.) ──
_MONATSNAME = "|".join(sorted(_MONATE, key=len, reverse=True))
_SPANNE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})\s*(?:-|–|—|bis)\s*(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")
_STICHWORT = (r"(?<![a-zäöüß])(?:abrechnungs(?:monat|zeitraum|periode)|abrechnung\s+(?:für|fuer|vom|monat)|"
              r"(?:lohn|gehalts|entgelt|verdienst|bezüge|bezuege)abrechnung(?:\s+(?:für|fuer))?|"
              r"bezüge\s+(?:für|fuer)|bezuege\s+(?:für|fuer)|(?:für|fuer)\s+(?:den\s+)?monat|"
              r"monat|zeitraum|periode)\s*(?::\s*)?")
_STICH_NAME = re.compile(_STICHWORT + r"(" + _MONATSNAME + r")\.?\s+(\d{4})(?!\d)", re.I)
# Nur „03/2024“, „3/24“ oder „03.2024“ – nicht „Monat 9.99“ (Betrag) oder „Zeitraum 3.21“.
_STICH_ZAHL = re.compile(_STICHWORT + r"(\d{1,2})\s*(?:/\s*(\d{4}|\d{2})|\.\s*(\d{4}))(?![\d.,])", re.I)
_NAME_JAHR = re.compile(r"(?<![\wäöü])(" + _MONATSNAME + r")\.?\s+(\d{4})(?!\d)", re.I)


def _jahr(j: str) -> int:
    return int(j) + (2000 if len(j) == 2 and int(j) <= 69 else 1900 if len(j) == 2 else 0)


def _monatsende(j: int, mo: int) -> date | None:
    try:
        return (date(j + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)) if 1 <= mo <= 12 else None
    except ValueError:
        return None


def finde_zeitraum(text: str, heute: date) -> date | None:
    """Ende des Abrechnungsmonats – nur mit Stichwort davor („Abrechnungsmonat März 2024“, „Bezüge für 03/2024“)
    oder als Monats-Spanne („01.03.2024 – 31.03.2024“). Ein bloßes „Stand März 2019“ zählt nicht."""
    text = re.sub(r"\s+", " ", (text or "")[:6000])

    def gut(d):
        return d if d and 1950 <= d.year and d <= heute + timedelta(days=45) else None
    for m in _STICH_NAME.finditer(text):
        if d := gut(_monatsende(int(m.group(2)), _MONATE.get(m.group(1).lower().rstrip("."), 0))):
            return d
    for m in _STICH_ZAHL.finditer(text):
        if d := gut(_monatsende(_jahr(m.group(2) or m.group(3)), int(m.group(1)))):
            return d
    for m in _SPANNE.finditer(text):       # nur Monats-Spannen – „Urlaubsjahr 01.01.–31.12.“ ist kein Abrechnungsmonat
        try:
            von = date(_jahr(m.group(3)), int(m.group(2)), int(m.group(1)))
            bis = date(_jahr(m.group(6)), int(m.group(5)), int(m.group(4)))
        except ValueError:
            continue
        if von <= bis and (bis - von).days <= 31 and gut(bis):
            return bis
    return None



MONATSNAMEN = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
               "November", "Dezember")


def monat_im_text(text: str) -> tuple[int, int] | None:
    """(Jahr, Monat) aus einem kurzen Text wie „März 2024“, „03/2024“, „3.24“ – z. B. aus einem alten Titel."""
    if m := _NAME_JAHR.search(text or ""):
        mo = _MONATE.get(m.group(1).lower().rstrip("."), 0)
        if mo:
            return int(m.group(2)), mo
    if m := re.search(r"(?<![\d.])(\d{1,2})\s*[./]\s*(\d{4}|\d{2})(?![\d.])", text or ""):
        mo = int(m.group(1))
        if 1 <= mo <= 12:
            return _jahr(m.group(2)), mo
    return None
