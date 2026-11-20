# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
from db import Db
from regeln import Regeln


def test_lernen_schreibweise_und_art(tmp_path):
    r = Regeln(Db(tmp_path / "r.sqlite"))
    assert r.kanon("Techniker Krankenkasse") == "Techniker Krankenkasse"
    assert r.art_fuer("TK") is None
    r.lerne("Techniker Krankenkasse", "TK", "Versicherungen")
    assert r.kanon("techniker krankenkasse") == "TK"
    assert r.art_fuer("tk") == "Versicherungen"
    assert r.art_fuer("Techniker Krankenkasse") == "Versicherungen"


def test_nur_art_korrigiert(tmp_path):
    r = Regeln(Db(tmp_path / "r.sqlite"))
    r.lerne("DRV", "DRV", "Rente")
    assert r.kanon("DRV") == "DRV"
    assert r.art_fuer("DRV") == "Rente"


def test_umlernen_ueberschreibt(tmp_path):
    r = Regeln(Db(tmp_path / "r.sqlite"))
    r.lerne("Allianz", "Allianz", "Versicherungen")
    r.lerne("Allianz", "Allianz", "Rente")
    assert r.art_fuer("Allianz") == "Rente"


def test_unbekannt_wird_nie_schreibweise(tmp_path):
    r = Regeln(Db(tmp_path / "r.sqlite"))
    r.lerne("Unbekannt", "TK", "Versicherungen")
    r.lerne("", "AOK", "Versicherungen")
    assert r.kanon("Unbekannt") == "Unbekannt"
    assert r.art_fuer("Unbekannt") is None
    assert r.art_fuer("TK") == "Versicherungen"
