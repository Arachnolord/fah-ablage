# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
from datetime import date
from pathlib import Path, PurePosixPath

import ablage
import config


def test_saeubere_entfernt_verbotene_zeichen():
    s = ablage.saeubere('TK / Techniker: Krankenkasse? "x" <y> |z| *')
    for z in '/\\:?"<>|*':
        assert z not in s
    assert "Techniker" in s


def test_saeubere_kuerzt_und_trimmt():
    s = ablage.saeubere("A" * 200 + " .")
    assert len(s) <= 80
    assert not s.endswith((".", " "))


def test_saeubere_leer_und_umlaute():
    assert ablage.saeubere("") == "Unbekannt"
    assert ablage.saeubere(" ... ") == "Unbekannt"
    assert ablage.saeubere("Größe Übersicht") == "Größe Übersicht"


def test_saeubere_keine_punktordner():
    assert ablage.saeubere("..") == "Unbekannt"
    assert not ablage.saeubere(".versteckt").startswith(".")


def test_zielpfad_beispiel():
    p = ablage.zielpfad("Versicherungen", "TK", date(2026, 11, 12), "Beitragsbescheid", "pdf")
    assert p == PurePosixPath("Versicherungen/TK/2026/2026-11-12 Beitragsbescheid.pdf")


def test_zielpfad_kein_ausbruch():
    p = ablage.zielpfad("../..", "../etc", date(2026, 1, 2), "../passwd", "pdf")
    assert ".." not in p.parts
    assert len(p.parts) == 4


def test_ablegen_konflikt_nie_ueberschreiben(tmp_path):
    archiv = tmp_path / "archiv"
    archiv.mkdir()
    rel = PurePosixPath("Versicherungen/TK/2026/2026-11-12 Beitragsbescheid.pdf")
    a = tmp_path / "a.pdf"
    a.write_bytes(b"erste")
    b = tmp_path / "b.pdf"
    b.write_bytes(b"zweite")
    r1 = ablage.ablegen(a, rel, archiv)
    r2 = ablage.ablegen(b, rel, archiv)
    assert r1 == rel
    assert r2 == PurePosixPath("Versicherungen/TK/2026/2026-11-12 Beitragsbescheid (2).pdf")
    assert (archiv / r1).read_bytes() == b"erste"
    assert (archiv / r2).read_bytes() == b"zweite"
    assert not a.exists() and not b.exists()


def test_verschieben_und_leere_ordner(tmp_path):
    archiv = tmp_path / "archiv"
    archiv.mkdir()
    q = tmp_path / "q.pdf"
    q.write_bytes(b"x")
    alt = ablage.ablegen(q, PurePosixPath("Sonstiges/Unbekannt/2026/2026-01-01 Brief.pdf"), archiv)
    neu = ablage.verschieben(alt, PurePosixPath("Versicherungen/TK/2026/2026-01-01 Brief.pdf"), archiv)
    assert (archiv / neu).read_bytes() == b"x"
    assert not (archiv / "Sonstiges").exists()   # leere Ordner weg
    assert archiv.exists()


def test_leere_ordner_entfernen_nie_archiv(tmp_path):
    archiv = tmp_path / "archiv"
    (archiv / "A" / "B").mkdir(parents=True)
    ablage.leere_ordner_entfernen(archiv / "A" / "B", archiv)
    assert archiv.exists() and not (archiv / "A").exists()


def test_einstellungen_standard_und_speichern():
    e = config.lade_einstellungen()
    assert e["arten"][0] == "Versicherungen"
    assert "Sonstiges" in e["arten"] and len(e["arten"]) == 15
    assert e["unsicher_schwelle"] == 0.75
    e["unsicher_schwelle"] = 0.6
    config.speichere_einstellungen(e)
    assert config.lade_einstellungen()["unsicher_schwelle"] == 0.6
    e["unsicher_schwelle"] = 0.75
    config.speichere_einstellungen(e)


def test_ablegen_abbruch_hinterlaesst_keinen_rest(tmp_path, monkeypatch):
    import errno
    import os as _os
    archiv = tmp_path / "archiv"
    archiv.mkdir()
    q = tmp_path / "q.pdf"
    q.write_bytes(b"inhalt")

    def kein_rename(a, b):
        raise OSError(errno.EXDEV, "andere Platte")

    def halb(a, b):
        Path(b).write_bytes(b"inh")
        raise OSError(errno.ENOSPC, "voll")
    monkeypatch.setattr(ablage.os, "rename", kein_rename)
    monkeypatch.setattr(ablage.shutil, "copyfile", halb)
    rel = PurePosixPath("A/B/2026/2026-01-01 X.pdf")
    import pytest
    with pytest.raises(OSError):
        ablage.ablegen(q, rel, archiv)
    assert q.read_bytes() == b"inhalt"
    assert not any(p.is_file() for p in archiv.rglob("*"))


def test_reste_entfernen(tmp_path):
    archiv = tmp_path / "archiv"
    (archiv / "A").mkdir(parents=True)
    (archiv / "A" / ".x.pdf.teil").write_bytes(b"halb")
    (archiv / "A" / "echt.pdf").write_bytes(b"ganz")
    ablage.reste_entfernen(archiv)
    assert [p.name for p in (archiv / "A").iterdir()] == ["echt.pdf"]
