# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

import sicherung
from db import Db
from regeln import Regeln


def _umgebung(tmp_path, name):
    u = {n: tmp_path / name / n for n in ("archiv", "daten")}
    for p in u.values():
        p.mkdir(parents=True)
    u["db"] = Db(u["daten"] / "ablage.sqlite")
    u["ziel"] = u["daten"] / "sicherungen"
    return u


def _befuellen(u):
    rel = "Versicherungen/TK/2026/2026-11-12 Beitragsbescheid.pdf"
    (u["archiv"] / rel).parent.mkdir(parents=True)
    (u["archiv"] / rel).write_bytes(b"%PDF-tk")
    (u["archiv"] / ".DS_Store").write_bytes(b"mac")
    u["db"].einfuegen(sha256="a", rel_pfad=rel, art="Versicherungen", absender="TK", datum="2026-11-12",
                      titel="Beitragsbescheid", unsicher=0, quelle="eingang", text="Ihr Beitragsbescheid 2027")
    korb = u["daten"] / "papierkorb" / "2.pdf"
    korb.parent.mkdir(parents=True)
    korb.write_bytes(b"%PDF-alt")
    i = u["db"].einfuegen(sha256="b", rel_pfad="Steuer/FA/2025/2025-01-01 Alt.pdf", art="Steuer", absender="FA",
                          datum="2025-01-01", titel="Alt", unsicher=0, quelle="import", text="alt")
    u["db"].aendern(i, status="papierkorb", papierkorb_pfad=str(korb), papierkorb_ts=123.0)
    Regeln(u["db"]).lerne("Techniker Krankenkasse", "TK", "Versicherungen")
    return rel


def test_erstellen_enthaelt_alles_ausser_versteckten(tmp_path):
    u = _umgebung(tmp_path, "alt")
    rel = _befuellen(u)
    p = sicherung.erstellen(u["db"], u["archiv"], u["daten"], u["ziel"], {"arten": ["Versicherungen", "Steuer"]})
    assert p.parent == u["ziel"] and p.name.startswith("fah-ablage-sicherung-") and p.suffix == ".tar"
    with tarfile.open(p) as t:
        namen = t.getnames()
        manifest = json.load(t.extractfile("manifest.json"))
    assert {"manifest.json", "ablage.sqlite", "einstellungen.json", f"archiv/{rel}", "papierkorb/2.pdf"} <= set(namen)
    assert not any(".DS_Store" in n for n in namen)
    assert manifest["format"] == 1 and manifest["dokumente"] == 2
    assert not list(u["ziel"].glob(".*"))                       # keine halbe Datei liegen geblieben


def test_wiederherstellen_in_frische_installation(tmp_path):
    alt = _umgebung(tmp_path, "alt")
    rel = _befuellen(alt)
    p = sicherung.erstellen(alt["db"], alt["archiv"], alt["daten"], alt["ziel"], {"arten": ["Versicherungen", "Steuer", "Haustiere"]})
    neu = _umgebung(tmp_path, "neu")
    gespeichert = {}
    erg = sicherung.wiederherstellen(p, neu["db"], neu["archiv"], neu["daten"], gespeichert.update)
    assert erg["dokumente"] == 2
    assert (neu["archiv"] / rel).read_bytes() == b"%PDF-tk"
    treffer, n = neu["db"].suche(text="beitrag")
    assert n == 1 and treffer[0]["absender"] == "TK"
    korb = [d for d in neu["db"].alle_aus_quelle("import")][0]
    assert korb["status"] == "papierkorb" and Path(korb["papierkorb_pfad"]).read_bytes() == b"%PDF-alt"
    assert Path(korb["papierkorb_pfad"]).parent == neu["daten"] / "papierkorb"
    assert Regeln(neu["db"]).kanon("Techniker Krankenkasse") == "TK"
    assert Regeln(neu["db"]).art_fuer("TK") == "Versicherungen"
    assert gespeichert["arten"] == ["Versicherungen", "Steuer", "Haustiere"]


def test_nur_in_leere_ablage(tmp_path):
    alt = _umgebung(tmp_path, "alt")
    _befuellen(alt)
    p = sicherung.erstellen(alt["db"], alt["archiv"], alt["daten"], alt["ziel"], {})
    with pytest.raises(sicherung.SicherungFehler, match="leer"):
        sicherung.wiederherstellen(p, alt["db"], alt["archiv"], alt["daten"], lambda e: None)


def _boese_tar(pfad, mitglieder):
    with tarfile.open(pfad, "w") as t:
        for name, daten, art in mitglieder:
            info = tarfile.TarInfo(name)
            if art == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                t.addfile(info)
            else:
                info.size = len(daten)
                t.addfile(info, io.BytesIO(daten))


def test_boesartige_sicherung_schreibt_nichts(tmp_path):
    alt = _umgebung(tmp_path, "alt")
    _befuellen(alt)
    gut = sicherung.erstellen(alt["db"], alt["archiv"], alt["daten"], alt["ziel"], {})
    with tarfile.open(gut) as t:
        db_bytes = t.extractfile("ablage.sqlite").read()
        manifest = t.extractfile("manifest.json").read()
    for boese in (("archiv/../../flucht.txt", b"x", "datei"), ("/etc/flucht", b"x", "datei"),
                  ("archiv/link", b"", "symlink")):
        neu = _umgebung(tmp_path, "neu-" + str(abs(hash(boese[0]))))
        p = tmp_path / "boese.tar"
        _boese_tar(p, [("manifest.json", manifest, "datei"), ("ablage.sqlite", db_bytes, "datei"), boese])
        with pytest.raises(sicherung.SicherungFehler):
            sicherung.wiederherstellen(p, neu["db"], neu["archiv"], neu["daten"], lambda e: None)
        assert not any(neu["archiv"].rglob("*")) and neu["db"].zahlen()["abgelegt"] == 0
    assert not (tmp_path / "flucht.txt").exists()


def test_kaputte_datenbank_schreibt_nichts(tmp_path):
    neu = _umgebung(tmp_path, "neu")
    p = tmp_path / "kaputt.tar"
    _boese_tar(p, [("manifest.json", json.dumps({"format": 1}).encode(), "datei"),
                   ("ablage.sqlite", b"keine datenbank", "datei"), ("archiv/A/x.pdf", b"x", "datei")])
    with pytest.raises(sicherung.SicherungFehler):
        sicherung.wiederherstellen(p, neu["db"], neu["archiv"], neu["daten"], lambda e: None)
    assert not any(neu["archiv"].rglob("*"))


def test_nur_die_drei_neuesten_bleiben(tmp_path):
    u = _umgebung(tmp_path, "alt")
    _befuellen(u)
    pfade = [sicherung.erstellen(u["db"], u["archiv"], u["daten"], u["ziel"], {}, zeit=f"20261112-10000{i}")
             for i in range(4)]
    assert sorted(p.name for p in u["ziel"].iterdir()) == sorted(p.name for p in pfade[1:])


def test_liste_und_namenspruefung(tmp_path):
    u = _umgebung(tmp_path, "alt")
    _befuellen(u)
    p = sicherung.erstellen(u["db"], u["archiv"], u["daten"], u["ziel"], {})
    liste = sicherung.liste(u["ziel"])
    assert liste[0]["name"] == p.name and liste[0]["groesse"] > 0
    assert sicherung.pfad_fuer(u["ziel"], p.name) == p
    for boese in ("../ablage.sqlite", "x.tar", "/etc/passwd", "fah-ablage-sicherung-../../x.tar"):
        assert sicherung.pfad_fuer(u["ziel"], boese) is None
