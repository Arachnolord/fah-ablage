# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Ordnerwahl für den Import: nur unterhalb der Wurzel, nie Eingang/Archiv/Daten, nichts Verstecktes."""
import os
import sqlite3

import pytest

from db import Db
from ordnerwahl import Quellen, normalisiere


@pytest.fixture
def baum(tmp_path):
    w = tmp_path / "home"
    for d in ("Dokumente/Privat/Steuer", "Dokumente/Ablage/Steuer", "Dokumente/Eingang", "Bilder",
              ".versteckt", "@eaDir", "#recycle", "zz-daten"):
        (w / d).mkdir(parents=True)
    (w / "Dokumente/Privat/Steuer/bescheid.pdf").write_bytes(b"a")
    (w / "Dokumente/Privat/brief.pdf").write_bytes(b"b")
    (w / "Dokumente/Privat/urlaub.mp4").write_bytes(b"v")
    (w / "Dokumente/Privat/.DS_Store").write_bytes(b"x")
    (w / "Dokumente/Ablage/Steuer/abgelegt.pdf").write_bytes(b"c")
    (w / "Dokumente/Eingang/neu.pdf").write_bytes(b"d")
    (w / "zz-daten/ablage.sqlite").write_bytes(b"e")
    (w / ".versteckt/geheim.pdf").write_bytes(b"f")
    draussen = tmp_path / "draussen"
    draussen.mkdir()
    (draussen / "fremd.pdf").write_bytes(b"g")
    os.symlink(draussen, w / "Dokumente/Verknuepfung")
    # Archiv per Identität gesperrt, Eingang per Anzeigepfad, Daten per Identität.
    q = Quellen(w, sperr_ordner=(w / "Dokumente/Ablage", w / "zz-daten"), sperr_rel=("Dokumente/Eingang",),
                anzeige="/home/benutzer")
    return w, q


def test_normalisiere():
    assert normalisiere("") == "" and normalisiere("/") == ""
    assert normalisiere("/Dokumente//Privat/") == "Dokumente/Privat"
    assert normalisiere("Dokumente\\Privat") == "Dokumente/Privat"
    for schlecht in ("../etc", "Dokumente/../../x", ".ssh", "Dokumente/@eaDir", "#recycle"):
        assert normalisiere(schlecht) is None


def test_unterordner_ohne_versteckte_gesperrte_und_links(baum):
    _, q = baum
    assert [o["name"] for o in q.unterordner("")] == ["Bilder", "Dokumente"]
    assert q.unterordner("Dokumente") == [{"name": "Privat", "pfad": "Dokumente/Privat"}]
    assert q.unterordner("../") is None and q.unterordner("gibtsnicht") is None


def test_aufloesen_sperrt_eingang_archiv_daten_und_links(baum):
    w, q = baum
    assert q.aufloesen("Dokumente/Privat") == w / "Dokumente/Privat"
    for gesperrt in ("Dokumente/Ablage", "Dokumente/Ablage/Steuer", "Dokumente/Eingang", "zz-daten",
                     "Dokumente/Verknuepfung", ".versteckt", "Dokumente/Privat/brief.pdf"):
        assert q.aufloesen(gesperrt) is None, gesperrt
    assert q.anzeige_pfad("Dokumente/Privat") == "/home/benutzer/Dokumente/Privat"
    assert q.anzeige_pfad("") == "/home/benutzer"


def test_dateien_auch_von_oben_ohne_ablage_eingang_medien(baum):
    w, q = baum
    gefunden = sorted((p.relative_to(w).as_posix(), h) for p, h in q.dateien(""))
    assert gefunden == [("Dokumente/Privat/Steuer/bescheid.pdf", "Dokumente/Privat/Steuer"),
                        ("Dokumente/Privat/brief.pdf", "Dokumente/Privat")]
    assert sorted(h for _, h in q.dateien("Dokumente/Privat")) == ["", "Steuer"]
    assert q.zaehlen("Dokumente/Privat") == (2, True)
    assert q.zaehlen("Dokumente/Privat", grenze=1) == (1, False)
    assert list(q.dateien("Dokumente/Ablage")) == []


def test_alte_datenbank_bekommt_ordner_spalte(tmp_path):
    pfad = tmp_path / "alt.sqlite"
    c = sqlite3.connect(pfad)
    c.executescript("""
        CREATE TABLE dokumente(id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, rel_pfad TEXT NOT NULL,
            art TEXT NOT NULL, absender TEXT NOT NULL, datum TEXT NOT NULL, titel TEXT NOT NULL,
            unsicher INTEGER NOT NULL DEFAULT 0, grund TEXT NOT NULL DEFAULT '', quelle TEXT NOT NULL DEFAULT 'eingang',
            herkunft TEXT NOT NULL DEFAULT '', eingang_ts REAL NOT NULL DEFAULT 0, sicherheit REAL NOT NULL DEFAULT 0,
            ms INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'abgelegt',
            papierkorb_pfad TEXT NOT NULL DEFAULT '', papierkorb_ts REAL NOT NULL DEFAULT 0);
        CREATE TABLE warteschlange(id INTEGER PRIMARY KEY, pfad TEXT UNIQUE NOT NULL, quelle TEXT NOT NULL,
            herkunft TEXT NOT NULL DEFAULT '', versuche INTEGER NOT NULL DEFAULT 0,
            naechster_ts REAL NOT NULL DEFAULT 0, eingereiht_ts REAL NOT NULL DEFAULT 0);
        INSERT INTO dokumente(sha256,rel_pfad,art,absender,datum,titel,quelle) VALUES('a','x.pdf','Steuer','FA','2024-01-01','B','import');
        INSERT INTO dokumente(sha256,rel_pfad,art,absender,datum,titel,quelle) VALUES('b','y.pdf','Steuer','FA','2024-01-01','B','upload');
    """)
    c.commit()
    c.close()
    d = Db(pfad)
    assert d.ordner_nachtragen("Dokumente/Privat") == 1
    assert d.anzahl_import("Dokumente/Privat") == 1
    assert [x["ordner"] for x in d.alle_aus_quelle("upload")] == [""]
    assert d.einreihen("/q/a.pdf", "import", "", "Scans") is True


def test_unlesbare_namen_werden_uebersprungen():
    from ordnerwahl import datei_ignorieren, lesbar, ordner_ignorieren
    kaputt = b"Rechnung_M\xfcller.pdf".decode("utf-8", "surrogateescape")
    assert not lesbar(kaputt) and datei_ignorieren(kaputt) and ordner_ignorieren(kaputt)
    assert lesbar("Müller.pdf") and not datei_ignorieren("Müller.pdf")


def test_wurzel_in_eingang_oder_archiv_schaltet_wahl_ab(tmp_path):
    q = Quellen(tmp_path, anzeige="/home/benutzer/Dokumente/Ablage",
                sperr_anzeige=("/home/benutzer/Dokumente/Eingang", "/home/benutzer/Dokumente/Ablage"))
    assert q.aufloesen("") is None and q.unterordner("") is None
    q = Quellen(tmp_path, sperr_ordner=(tmp_path,))
    assert q.aufloesen("") is None


def test_nachtragen_nur_einmal_und_alte_auftraege_umschreiben(tmp_path):
    d = Db(tmp_path / "t.sqlite")
    d.einfuegen(sha256="a", rel_pfad="x.pdf", art="Steuer", absender="FA", datum="2024-01-01", titel="B",
                quelle="import", ordner="")
    d._x("INSERT INTO warteschlange(pfad,quelle,herkunft) VALUES('/import/Steuer/b.pdf','import','Steuer')")
    d.setze_zaehler("ordner_nachtragen", 1)           # so steht es nach der Nachrüstung einer 1.5-Datenbank
    assert d.ordner_nachtragen("Dokumente/Privat", "/quellen") == 1
    job = d.naechster()
    assert job["pfad"] == "/quellen/Dokumente/Privat/Steuer/b.pdf" and job["ordner"] == "Dokumente/Privat"
    # Danach ist ordner='' ein Import „ganz oben“ und bleibt das auch nach jedem Neustart.
    d.einfuegen(sha256="c", rel_pfad="y.pdf", art="Steuer", absender="FA", datum="2024-01-01", titel="C",
                quelle="import", ordner="")
    assert d.ordner_nachtragen("Dokumente/Privat", "/quellen") == 0
    assert Db(tmp_path / "t.sqlite").ordner_nachtragen("Dokumente/Privat") == 0
    assert d.anzahl_import("") == 1 and d.anzahl_import("Dokumente/Privat") == 1
