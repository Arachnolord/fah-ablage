# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Einheitliche Titel. Lohnzettel heißen immer „Gehaltsabrechnung <Monat> <Jahr>“ (Betreiber 13.11.2026)."""
import re
from datetime import date

from datum import _MONATSNAME, MONATSNAMEN, finde_zeitraum, monat_im_text

# Lohn-/Gehaltszettel in allen gängigen Schreibweisen – aber nie Lohnsteuerbescheinigung, Jahresmeldung o. Ä.
_STAMM = r"(?:gehalts?|lohn|entgelte?|verdienst|bezüge|bezuege|besoldungs?)"
_GEHALT = re.compile(
    r"^\s*(?:(?:brutto[\s/-]*(?:und\s+)?netto[\s/-]*)?(?:lohn-?\s+und\s+)?" + _STAMM +
    r"[\s-]*abrechnung|(?:lohn|gehalts?)[\s-]*(?:zettel|schein)|entgelt[\s-]*bescheinigung|bezüge[\s-]*mitteilung"
    r"|brutto[\s/-]*(?:und\s+)?netto[\s/-]*(?:abrechnung|bezüge|bezuege)"
    r"|(?:monats)?abrechnung\s+(?:der\s+|des\s+|über\s+)?(?:brutto[\s/-]*(?:und\s+)?netto[\s/-]*)?" + _STAMM + r"s?"
    r")(?![a-zäöüß])", re.I)
_NICHT = re.compile(r"steuer|jahres|sozialversicherung|meldung|korrektur", re.I)


# Nach dem Lohnzettel-Wort darf nur noch Monat/Jahr stehen – sonst ginge beim Umbenennen etwas verloren.
_REST = re.compile(r"^[\s\d./,()-]*(?:(?:für|fuer|vom|monat|" + _MONATSNAME + r")\.?[\s\d./,()-]*)*$", re.I)


def ist_gehaltsabrechnung(titel: str) -> bool:
    m = _GEHALT.search(titel or "")
    return bool(m) and not _NICHT.search(titel) and bool(_REST.match(titel[m.end():]))


def gehaltsabrechnung(titel: str, text: str, datum: date, heute: date) -> str | None:
    """Neuer Titel für einen Lohnzettel, sonst None. Monat: Abrechnungsmonat aus dem Text, sonst aus dem
    bisherigen Titel, sonst aus dem Datum des Dokuments."""
    if not ist_gehaltsabrechnung(titel):
        return None
    zeitraum = finde_zeitraum(text, heute)
    if zeitraum:
        jahr, monat = zeitraum.year, zeitraum.month
    else:
        jahr, monat = monat_im_text(_GEHALT.sub("", titel, count=1)) or (datum.year, datum.month)
    neu = f"Gehaltsabrechnung {MONATSNAMEN[monat - 1]} {jahr}"
    return neu if neu != titel else None
