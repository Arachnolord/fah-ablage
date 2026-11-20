# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Zweiter Blick auf unsichere Dokumente (Betreiber 13.11.2026: 100 Lohnzettel von Hand geprüft).

Stufe 1 – ähnliche Dokumente (schnell, läuft sofort und nach jeder Korrektur erneut): Wie wurden fast gleiche,
schon sichere Dokumente eingeordnet? Lohnzettel desselben Arbeitgebers sind fast wortgleich. Gemessen auf der
echten Ablage (499 Dokumente, jedes gegen alle anderen): mindestens zwei Nachbarn mit Ähnlichkeit ≥ 0,6 und
≥ 80 % davon einig → Art in 99,6 % richtig, für knapp die Hälfte aller Dokumente. Ein einzelner Nachbar
reicht nicht (dort lagen die Fehler).

Stufe 2 – die KI noch einmal genauer (langsam, einmal je Dokument, nur wenn sonst nichts zu tun ist): mehr Text,
die ähnlichsten geprüften Dokumente als Vorbild. Ihr Ergebnis zählt nur, wenn etwas Unabhängiges es stützt.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass

STARK = 0.6            # Ähnlichkeit für „fast dasselbe Dokument“
SCHWACH = 0.35         # Ähnlichkeit, ab der ein Dokument als Vorbild für die KI taugt
MIN_STARK = 2
EINIG = 0.8
MIN_WOERTER = 15
KANDIDATEN = 40

# Gründe, die eine bessere Einordnung der ART auflöst – im Gegensatz zu Datum oder Lesbarkeit.
ART_GRUENDE = {"KI unsicher", "keine passende Art", "anders als frühere Briefe dieses Absenders",
               "Regel weicht von KI ab", "KI-Antwort unbrauchbar"}
DATUM_GRUND = "kein Datum erkannt"

_WORT = re.compile(r"[a-zäöüß]{3,}")


def woerter(text: str) -> frozenset:
    return frozenset(_WORT.findall((text or "")[:30000].casefold()))


def aehnlichkeit(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def suchwoerter(w: frozenset, n: int = 60) -> list[str]:
    """Für die Volltext-Vorauswahl: lange Wörter zuerst (die sind seltener und trennen besser)."""
    return sorted(w, key=lambda x: (-len(x), x))[:n]


@dataclass
class Nachbar:
    aehnlich: float
    id: int
    art: str
    absender: str
    titel: str


def absender_wechsel_ok(alt: str, neu: str, text: str, kanon=lambda s: s) -> bool:
    """Einen erkannten Absender nur ersetzen, wenn er Platzhalter ist, derselbe in anderer Schreibweise, oder der
    neue wörtlich im Text steht – sonst landete der Lohnzettel eines Arbeitgebers bei einem anderen mit
    derselben Lohnsoftware."""
    if not neu or neu.casefold() == alt.casefold():
        return False
    if alt.strip().casefold() in ("", "unbekannt") or kanon(alt).casefold() == neu.casefold():
        return True
    return len(neu) >= 3 and " ".join(neu.split()).casefold() in " ".join((text or "").split()).casefold()


def gruende_von(grund: str) -> list[str]:
    return [g for g in (grund or "").split("; ") if g]


@dataclass
class Urteil:
    art: str
    absender: str | None    # None: Absender nicht eindeutig, bisherigen behalten
    n: int


def urteil_nachbarn(nachbarn: list[Nachbar]) -> Urteil | None:
    """Stufe 1: nur bei mindestens zwei fast gleichen Dokumenten, die sich (fast) einig sind."""
    stark = [n for n in sorted(nachbarn, key=lambda n: -n.aehnlich)[:5] if n.aehnlich >= STARK]
    if len(stark) < MIN_STARK:
        return None
    art, k = Counter(n.art for n in stark).most_common(1)[0]
    if k / len(stark) < EINIG:
        return None
    gleiche = [n for n in stark if n.art == art]
    ab, j = Counter(n.absender.casefold() for n in gleiche).most_common(1)[0]
    absender = next(n.absender for n in gleiche if n.absender.casefold() == ab) if j / len(gleiche) >= EINIG else None
    return Urteil(art, absender, len(stark))


def mehrheit_schwach(nachbarn: list[Nachbar]) -> str | None:
    """Die Art, die ähnliche Dokumente (ab SCHWACH) mehrheitlich haben – als Stütze für die KI in Stufe 2."""
    schwach = [n for n in sorted(nachbarn, key=lambda n: -n.aehnlich)[:5] if n.aehnlich >= SCHWACH]
    if not schwach:
        return None
    art, k = Counter(n.art for n in schwach).most_common(1)[0]
    return art if k / len(schwach) >= 2 / 3 else None


def urteil_ki(ki_art: str, bisher_art: str, gruende: list[str], stuetzen: set[str], ki_sicher: bool) -> bool:
    """Stufe 2: Gilt die Art der zweiten KI-Runde als geklärt?
    stuetzen: Arten aus unabhängigen, GEPRÜFTEN Quellen (ähnliche Dokumente, gelernte Regel, frühere Briefe des
    Absenders – nie unsichere oder selbst nur maschinell geklärte). ki_sicher: KI-Sicherheit ≥ Schwelle."""
    if not ki_sicher:
        return False
    if ki_art in stuetzen:
        return True
    art_gruende = [g for g in gruende if g in ART_GRUENDE]
    # Ohne jede Stütze nur, wenn beide Runden dasselbe sagen und nichts dagegen spricht.
    return (not stuetzen and ki_art == bisher_art and ki_art != "Sonstiges"
            and set(art_gruende) <= {"KI unsicher", "KI-Antwort unbrauchbar"})
