# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
from datetime import date

from datum import finde_briefdatum

HEUTE = date(2026, 11, 12)


def test_ort_und_monatsname():
    assert finde_briefdatum("Techniker Krankenkasse\nBerlin, 3. März 2025\nBetreff", HEUTE) == date(2025, 3, 3)


def test_label_schlaegt_geburtsdatum():
    t = "Versicherter: Max\nGeburtsdatum 01.01.1960\nDatum: 05.02.2026\nIhr Beitrag"
    assert finde_briefdatum(t, HEUTE) == date(2026, 2, 5)


def test_iso_und_kurzes_jahr():
    assert finde_briefdatum("Stand 2026-10-01", HEUTE) == date(2026, 10, 1)
    assert finde_briefdatum("Musterstadt, 07.09.26", HEUTE) == date(2026, 9, 7)


def test_kein_datum_und_ungueltig():
    assert finde_briefdatum("Kein Datum hier", HEUTE) is None
    assert finde_briefdatum("31.02.2026", HEUTE) is None


def test_zukunft_und_uralt_verworfen():
    assert finde_briefdatum("gültig bis 31.12.2030", HEUTE) is None
    assert finde_briefdatum("seit 01.01.1901", HEUTE) is None
