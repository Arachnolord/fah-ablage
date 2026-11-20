# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Aus Korrekturen gelernte Regeln: Schreibweise → Absender, Absender → Art."""


def _schluessel(s: str) -> str:
    return " ".join((s or "").split()).casefold()


PLATZHALTER = {"", "unbekannt"}


class Regeln:
    def __init__(self, db):
        self.db = db

    def kanon(self, absender: str) -> str:
        if _schluessel(absender) in PLATZHALTER:
            return absender
        return self.db.alias(_schluessel(absender)) or absender

    def art_fuer(self, absender: str) -> str | None:
        return self.db.regel_art(_schluessel(self.kanon(absender)))

    def lerne(self, alt_absender: str, neu_absender: str, neue_art: str) -> None:
        # Platzhalter nie als Schreibweise lernen – sonst landete jeder unlesbare Brief beim neuen Absender.
        if alt_absender and _schluessel(alt_absender) not in PLATZHALTER \
                and _schluessel(alt_absender) != _schluessel(neu_absender):
            self.db.setze_alias(_schluessel(alt_absender), neu_absender)
        self.db.setze_regel_art(_schluessel(neu_absender), neue_art)
