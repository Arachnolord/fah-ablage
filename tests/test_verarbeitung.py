# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
import logging
import os
from datetime import date
from pathlib import Path

import pytest

import ki
from config import STANDARD_ARTEN
from db import Db
from extrakt import Ergebnis
from regeln import Regeln
from verarbeitung import Worker


class Uhr:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def lies_fake(pfad, arbeit):
    return Ergebnis(text="Techniker Krankenkasse Berlin, 12.11.2026 Beitragsbescheid",
                    ablage_datei=Path(pfad), endung="pdf")


def ki_fake(**ueber):
    def f(text, dateiname, arten, absender_bekannt, url, timeout=300, hinweise=None):
        f.hinweise = hinweise
        werte = dict(art="Versicherungen", absender="TK", datum=date(2026, 11, 12),
                     titel="Beitragsbescheid", sicherheit=0.95)
        werte.update(ueber)
        return ki.Einordnung(**werte)
    return f


@pytest.fixture
def umgebung(tmp_path):
    d = {n: tmp_path / n for n in ("eingang", "archiv", "import", "daten")}
    for p in d.values():
        p.mkdir()
    d["quellen"] = tmp_path        # Wurzel der Ordnerwahl – enthält absichtlich auch Eingang/Archiv/Daten
    d["db"] = Db(d["daten"] / "t.sqlite")
    d["uhr"] = Uhr()
    return d


def _worker(u, ki_fn=None, lies_fn=lies_fake, schwelle=0.75):
    return Worker(u["db"], lambda: {"arten": STANDARD_ARTEN, "unsicher_schwelle": schwelle},
                  ki_fn=ki_fn or ki_fake(), lies_fn=lies_fn, eingang=u["eingang"], archiv=u["archiv"],
                  quellen=u["quellen"], daten=u["daten"], ki_url="http://ki", uhr=u["uhr"],
                  heute=lambda: date(2026, 11, 12), ki_ok_fn=lambda: True)


def _bereit(w, u):
    """Zwei Scans mit 60 s Ruhe dazwischen = stabil."""
    w.scan_eingang()
    u["uhr"].t += 60
    w.scan_eingang()


def test_eingang_wird_abgelegt(umgebung):
    u = umgebung
    (u["eingang"] / "scan 1.pdf").write_bytes(b"%PDF-1\n%%EOF\n")
    w = _worker(u)
    _bereit(w, u)
    assert w.schritt() is True
    ziel = u["archiv"] / "Versicherungen/TK/2026/2026-11-12 Beitragsbescheid.pdf"
    assert ziel.read_bytes() == b"%PDF-1\n%%EOF\n"
    assert not (u["eingang"] / "scan 1.pdf").exists()
    treffer, n = u["db"].suche(text="beitragsbescheid")
    assert n == 1 and treffer[0]["unsicher"] == 0 and treffer[0]["quelle"] == "eingang"
    assert w.schritt() is False


def test_wachsende_datei_wartet(umgebung):
    u = umgebung
    f = u["eingang"] / "gross.pdf"
    f.write_bytes(b"%PDF")
    w = _worker(u)
    w.scan_eingang()
    with open(f, "ab") as h:
        h.write(b"mehr")
    u["uhr"].t += 60
    w.scan_eingang()
    assert u["db"].anzahl() == 0
    u["uhr"].t += 30
    w.scan_eingang()
    assert u["db"].anzahl() == 0                  # erst 30 s ruhig
    u["uhr"].t += 30
    w.scan_eingang()
    assert u["db"].anzahl() == 1


def test_versteckte_und_teildateien_ignoriert(umgebung):
    u = umgebung
    for n in (".DS_Store", "x.part", "y.tmp", "._scan.pdf"):
        (u["eingang"] / n).write_bytes(b"x")
    w = _worker(u)
    _bereit(w, u)
    assert u["db"].anzahl() == 0


def test_dublette(umgebung):
    u = umgebung
    w = _worker(u)
    (u["eingang"] / "a.pdf").write_bytes(b"gleich")
    _bereit(w, u)
    w.schritt()
    (u["eingang"] / "b.pdf").write_bytes(b"gleich")
    _bereit(w, u)
    w.schritt()
    assert u["db"].zahlen()["abgelegt"] == 1
    assert u["db"].zahlen()["dubletten"] == 1
    assert not (u["eingang"] / "b.pdf").exists()
    assert len(list((u["daten"] / "dubletten").iterdir())) == 1


def test_ki_dreimal_weg_dann_sonstiges(umgebung):
    u = umgebung

    def kaputt(*a, **k):
        raise ki.KiFehler("weg")
    w = _worker(u, ki_fn=kaputt)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    for _ in range(3):
        assert w.schritt() is True
        assert u["db"].zahlen()["abgelegt"] == 0
        assert w.schritt() is False            # wartet auf den nächsten Versuch
        u["uhr"].t += 3600
    assert w.schritt() is True
    d = u["db"].suche()[0][0]
    assert (d["art"], d["absender"], d["unsicher"]) == ("Sonstiges", "Unbekannt", 1)
    assert "KI" in d["grund"]
    assert (u["archiv"] / d["rel_pfad"]).exists()


def test_regel_schlaegt_ki(umgebung):
    u = umgebung
    Regeln(u["db"]).lerne("Techniker Krankenkasse", "TK", "Gesundheit")
    w = _worker(u, ki_fn=ki_fake(absender="Techniker Krankenkasse", sicherheit=0.3))
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    d = u["db"].suche()[0][0]
    assert (d["art"], d["absender"], d["unsicher"]) == ("Gesundheit", "TK", 1)
    assert "Regel weicht von KI ab" in d["grund"]


def test_unsicher_markiert_aber_abgelegt(umgebung):
    u = umgebung
    w = _worker(u, ki_fn=ki_fake(sicherheit=0.4, datum=None),
                lies_fn=lambda p, a: Ergebnis(text="ohne datum", ablage_datei=Path(p), endung="pdf"))
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    d = u["db"].suche()[0][0]
    assert d["unsicher"] == 1 and d["datum"] == "2027-01-15"   # Eingangsdatum aus der Uhr
    assert (u["archiv"] / d["rel_pfad"]).exists()


def test_import_kopiert_und_merkt_herkunft(umgebung):
    u = umgebung
    (u["import"] / "Versicherung" / "TK").mkdir(parents=True)
    q = u["import"] / "Versicherung" / "TK" / "brief.pdf"
    q.write_bytes(b"original")
    (u["import"] / ".stignore").write_text("x")
    w = _worker(u)
    assert w.import_starten("import") == 1
    w.schritt()
    assert q.read_bytes() == b"original"            # Quelle unverändert
    d = u["db"].suche()[0][0]
    assert d["quelle"] == "import" and d["herkunft"] == "Versicherung/TK"
    assert w.status()["import_fertig"] == 1 and w.status()["import_gesamt"] == 1
    assert list((u["daten"] / "arbeit").iterdir()) == []


def test_eingang_vor_import(umgebung):
    u = umgebung
    (u["import"] / "a.pdf").write_bytes(b"imp")
    w = _worker(u)
    w.import_starten("import")
    (u["eingang"] / "b.pdf").write_bytes(b"ein")
    _bereit(w, u)
    w.schritt()
    assert u["db"].suche()[0][0]["quelle"] == "eingang"


def test_kaputte_datei_wird_trotzdem_abgelegt(umgebung):
    u = umgebung
    w = _worker(u, lies_fn=lambda p, a: Ergebnis(text="", ablage_datei=Path(p), endung="pdf",
                                                   fehler="PDF nicht lesbar (Code 1)"))
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    d = u["db"].suche()[0][0]
    assert d["unsicher"] == 1 and "nicht lesbar" in d["grund"]


def test_unerwarteter_fehler_keine_endlosschleife(umgebung):
    u = umgebung

    def boom(p, a):
        raise RuntimeError("geheimer Inhalt")
    w = _worker(u, lies_fn=boom)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    for _ in range(4):
        w.schritt()
        u["uhr"].t += 3600
    assert u["db"].anzahl() == 0
    assert len(list((u["daten"] / "fehler").iterdir())) == 1


def test_log_ohne_inhalt(umgebung, caplog):
    u = umgebung
    caplog.set_level(logging.DEBUG)
    w = _worker(u, ki_fn=ki_fake(absender="Geheimabsender", titel="Geheimtitel"))
    (u["eingang"] / "Geheimname.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    for wort in ("Geheimabsender", "Geheimtitel", "Geheimname", "Techniker"):
        assert wort not in caplog.text
    assert "dok=" in caplog.text


def test_papierkorb_nach_30_tagen_leer(umgebung):
    u = umgebung
    w = _worker(u)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    i = u["db"].suche()[0][0]["id"]
    w.in_papierkorb(i)
    assert u["db"].suche()[1] == 0
    assert not any(p.is_file() for p in u["archiv"].rglob("*"))
    u["uhr"].t += 31 * 86400
    w.papierkorb_aufraeumen()
    assert u["db"].hole(i) is None
    assert list((u["daten"] / "papierkorb").iterdir()) == []


def test_papierkorb_wiederherstellen(umgebung):
    u = umgebung
    w = _worker(u)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    i = u["db"].suche()[0][0]["id"]
    rel = u["db"].hole(i)["rel_pfad"]
    w.in_papierkorb(i)
    w.wiederherstellen(i)
    assert (u["archiv"] / rel).exists() and u["db"].hole(i)["status"] == "abgelegt"


def test_sonstiges_ist_unsicher(umgebung):
    u = umgebung
    w = _worker(u, ki_fn=ki_fake(art="Sonstiges", sicherheit=0.99))
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    d = u["db"].suche()[0][0]
    assert d["unsicher"] == 1 and "keine passende Art" in d["grund"]


def test_kaum_text_ist_unsicher(umgebung):
    u = umgebung
    w = _worker(u, lies_fn=lambda p, a: Ergebnis(text=" ab \n", ablage_datei=Path(p), endung="pdf"))
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    d = u["db"].suche()[0][0]
    assert d["unsicher"] == 1 and "kaum Text" in d["grund"]


def test_abweichung_von_frueheren_briefen(umgebung):
    u = umgebung
    for i in range(2):
        u["db"].einfuegen(sha256=f"alt{i}", rel_pfad=f"Versicherungen/TK/2025/x{i}.pdf", art="Versicherungen",
                          absender="TK", datum="2025-01-01", titel="Brief", unsicher=0, quelle="eingang")
    w = _worker(u, ki_fn=ki_fake(art="Gesundheit", sicherheit=0.99))
    (u["eingang"] / "a.pdf").write_bytes(b"neu")
    _bereit(w, u)
    w.schritt()
    d = u["db"].suche(art="Gesundheit")[0][0]
    assert d["unsicher"] == 1 and "anders als frühere" in d["grund"]


def test_gleiche_art_wie_frueher_bleibt_sicher(umgebung):
    u = umgebung
    for i in range(2):
        u["db"].einfuegen(sha256=f"alt{i}", rel_pfad=f"Versicherungen/TK/2025/x{i}.pdf", art="Versicherungen",
                          absender="TK", datum="2025-01-01", titel="Brief", unsicher=0, quelle="eingang")
    w = _worker(u)
    (u["eingang"] / "a.pdf").write_bytes(b"neu")
    _bereit(w, u)
    w.schritt()
    assert u["db"].suche(nur_unsicher=True)[1] == 0


# ── Befunde der Prüfung ─────────────────────────────────────────────────
def test_regel_gleich_ki_bleibt_sicher(umgebung):
    u = umgebung
    Regeln(u["db"]).lerne("Techniker Krankenkasse", "TK", "Versicherungen")
    w = _worker(u, ki_fn=ki_fake(absender="Techniker Krankenkasse", sicherheit=0.3))
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    assert u["db"].suche()[0][0]["unsicher"] == 0


def test_quelle_geaendert_bleibt_im_eingang(umgebung):
    u = umgebung
    q = u["eingang"] / "a.pdf"
    q.write_bytes(b"teil1")

    def lies_und_weiterschreiben(p, a):
        with open(q, "ab") as h:
            h.write(b"teil2")
        return Ergebnis("Text " * 20, Path(p), "pdf")
    w = _worker(u, lies_fn=lies_und_weiterschreiben)
    _bereit(w, u)
    w.schritt()
    assert q.exists() and q.read_bytes() == b"teil1teil2"     # nichts verloren
    assert u["db"].zahlen()["abgelegt"] == 1


def test_pdf_ohne_ende_wird_spaeter_versucht(umgebung):
    u = umgebung
    (u["eingang"] / "halb.pdf").write_bytes(b"%PDF-1.4 halb")
    w = _worker(u)
    _bereit(w, u)
    for _ in range(2):
        w.schritt()
        assert u["db"].zahlen()["abgelegt"] == 0
        u["uhr"].t += 3600
    w.schritt()
    assert u["db"].zahlen()["abgelegt"] == 1          # nach 3 Versuchen trotzdem abgelegt


def test_absturz_zaehlt_versuch_vorher(umgebung):
    u = umgebung

    def absturz(p, a):
        raise SystemExit("kill")
    w = _worker(u, lies_fn=absturz)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    with pytest.raises(SystemExit):
        w.schritt()
    job = u["db"].naechster(jetzt=10**12)
    assert job["versuche"] == 1 and job["naechster_ts"] > u["uhr"].t


def test_quarantaene_fehler_blockiert_nicht(umgebung, monkeypatch):
    u = umgebung
    import verarbeitung

    def boom(p, a):
        raise RuntimeError("x")
    w = _worker(u, lies_fn=boom)
    echt = verarbeitung.shutil.move

    def move(q, z):
        if "/fehler/" in str(z):
            raise OSError("voll")
        return echt(q, z)
    monkeypatch.setattr(verarbeitung.shutil, "move", move)
    (u["eingang"] / "kaputt.pdf").write_bytes(b"x")
    _bereit(w, u)
    for _ in range(3):
        w.schritt()
        u["uhr"].t += 3600
    assert u["db"].anzahl() == 0


def test_ki_weg_pausiert_ohne_zaehlen(umgebung):
    u = umgebung
    ki_da = {"ja": False}
    w = _worker(u)
    w.ki_ok_fn = lambda: ki_da["ja"]
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    for _ in range(10):
        assert w.schritt() is False
        u["uhr"].t += 3600
    assert u["db"].hole_auftrag(u["db"].naechster(jetzt=10**12)["id"])["versuche"] == 0
    ki_da["ja"] = True
    u["uhr"].t += 3600
    assert w.schritt() is True
    assert u["db"].suche()[0][0]["art"] == "Versicherungen"


def test_ki_bricht_mitten_ab_zaehlt_nicht(umgebung):
    u = umgebung

    def weg(*a, **k):
        raise ki.KiNichtErreichbar("weg")
    w = _worker(u, ki_fn=weg)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    job = u["db"].naechster(jetzt=10**12)
    assert job["versuche"] == 0 and u["db"].zahlen()["abgelegt"] == 0


def test_korrektur_wartet_nicht_auf_ki(umgebung):
    import threading
    u = umgebung
    w0 = _worker(u)
    (u["eingang"] / "erst.pdf").write_bytes(b"1")
    _bereit(w0, u)
    w0.schritt()
    alt = u["db"].suche()[0][0]
    frei = threading.Event()

    def langsam(*a, **k):
        frei.wait(10)
        return ki_fake()(*a, **k)
    w = _worker(u, ki_fn=langsam)
    (u["eingang"] / "zweit.pdf").write_bytes(b"2")
    _bereit(w, u)
    t = threading.Thread(target=w.schritt)
    t.start()
    try:
        fertig = threading.Event()
        threading.Thread(target=lambda: (w.korrigieren(alt["id"], "Steuer", "Finanzamt", date(2026, 1, 1), "Bescheid"),
                                         fertig.set())).start()
        assert fertig.wait(3), "Korrektur hing hinter der KI"
    finally:
        frei.set()
        t.join(10)


def test_import_ohne_datum_nimmt_dateidatum(umgebung):
    u = umgebung
    q = u["import"] / "alt.pdf"
    q.write_bytes(b"alt")
    os.utime(q, (1557000000, 1557000000))           # 04./05.05.2019
    w = _worker(u, ki_fn=ki_fake(datum=None),
                lies_fn=lambda p, a: Ergebnis("ohne Datum " * 10, Path(p), "pdf"))
    w.import_starten("import")
    w.schritt()
    assert u["db"].suche()[0][0]["datum"].startswith("2019-05-0")


def test_thumbs_und_desktop_ini_ignoriert(umgebung):
    u = umgebung
    for n in ("Thumbs.db", "desktop.ini"):
        (u["eingang"] / n).write_bytes(b"x")
        (u["import"] / n).write_bytes(b"x")
    w = _worker(u)
    _bereit(w, u)
    assert w.import_starten("import") == 0 and u["db"].anzahl() == 0


def test_import_zuruecksetzen(umgebung):
    u = umgebung
    (u["import"] / "Steuer").mkdir()
    q = u["import"] / "Steuer" / "a.pdf"
    q.write_bytes(b"imp")
    w = _worker(u)
    w.import_starten("import")
    w.schritt()
    (u["eingang"] / "b.pdf").write_bytes(b"ein")
    _bereit(w, u)
    w.schritt()
    imp = u["db"].suche(sortierung="eingang", absteigend=False)[0][0]
    assert imp["quelle"] == "import"
    assert w.import_zuruecksetzen() == 1
    assert u["db"].hole(imp["id"]) is None
    assert not (u["archiv"] / imp["rel_pfad"]).exists()
    assert u["db"].zahlen()["abgelegt"] == 1 and q.read_bytes() == b"imp"
    assert w.status()["import_gesamt"] == 0


def test_eigene_arten_und_hinweise_gehen_an_die_ki(umgebung):
    u = umgebung
    fake = ki_fake(art="Haustiere")
    w = Worker(u["db"], lambda: {"arten": ["Haustiere", "Sonstiges"], "unsicher_schwelle": 0.75,
                                 "art_hinweise": {"Haustiere": "Tierarzt, Hundesteuer"}},
               ki_fn=fake, lies_fn=lies_fake, eingang=u["eingang"], archiv=u["archiv"], quellen=u["quellen"],
               daten=u["daten"], ki_url="http://ki", uhr=u["uhr"], heute=lambda: date(2026, 11, 12),
               ki_ok_fn=lambda: True)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    assert fake.hinweise == {"Haustiere": "Tierarzt, Hundesteuer"}
    assert u["db"].suche()[0][0]["art"] == "Haustiere"


def test_art_umbenennen_verschiebt_ordner_und_regeln(umgebung):
    u = umgebung
    w = _worker(u)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    Regeln(u["db"]).lerne("Techniker Krankenkasse", "TK", "Versicherungen")
    w.art_umbenennen("Versicherungen", "Krankenkasse")
    d = u["db"].suche()[0][0]
    assert d["art"] == "Krankenkasse" and d["rel_pfad"].startswith("Krankenkasse/TK/2026/")
    assert (u["archiv"] / d["rel_pfad"]).exists() and not (u["archiv"] / "Versicherungen").exists()
    assert Regeln(u["db"]).art_fuer("TK") == "Krankenkasse"


def test_art_umbenennen_in_vorhandenen_ordner_abgelehnt(umgebung):
    u = umgebung
    w = _worker(u)
    (u["eingang"] / "a.pdf").write_bytes(b"x")
    _bereit(w, u)
    w.schritt()
    (u["archiv"] / "Steuer").mkdir()
    with pytest.raises(ValueError):
        w.art_umbenennen("Versicherungen", "Steuer")
    assert u["db"].suche()[0][0]["art"] == "Versicherungen"


def test_import_von_ganz_oben_laesst_eingang_archiv_daten_aus(umgebung):
    u = umgebung
    (u["import"] / "a.pdf").write_bytes(b"imp")
    (u["archiv"] / "Steuer").mkdir()
    (u["archiv"] / "Steuer" / "abgelegt.pdf").write_bytes(b"arch")
    (u["eingang"] / "neu.pdf").write_bytes(b"ein")
    w = _worker(u)
    (u["daten"] / "papierkorb" / "1.pdf").write_bytes(b"korb")
    assert w.import_starten("") == 1
    with pytest.raises(FileNotFoundError):
        w.import_starten("archiv")
    with pytest.raises(FileNotFoundError):
        w.import_starten("../")


def test_zwei_ordner_getrennt_zuruecksetzen(umgebung):
    u = umgebung
    (u["quellen"] / "scans").mkdir()
    (u["import"] / "a.pdf").write_bytes(b"alt")
    (u["quellen"] / "scans" / "b.pdf").write_bytes(b"scan")
    w = _worker(u)
    assert w.import_starten("import") == 1
    assert w.import_starten("scans") == 1          # läuft noch: hinten dran
    assert w.status()["import_gesamt"] == 2
    w.schritt()
    w.schritt()
    assert u["db"].anzahl_import("import") == 1 and u["db"].anzahl_import("scans") == 1
    assert w.import_zuruecksetzen("scans") == 1
    assert u["db"].anzahl_import("import") == 1 and u["db"].anzahl_import("scans") == 0
    assert (u["quellen"] / "scans" / "b.pdf").read_bytes() == b"scan"


def test_zuruecksetzen_eines_ordners_waehrend_anderer_laeuft(umgebung):
    u = umgebung
    (u["quellen"] / "scans").mkdir()
    (u["import"] / "a.pdf").write_bytes(b"alt")
    (u["quellen"] / "scans" / "b.pdf").write_bytes(b"scan")
    (u["quellen"] / "scans" / "c.pdf").write_bytes(b"scan2")
    w = _worker(u)
    w.import_starten("import")
    w.import_starten("scans")
    assert w.import_zuruecksetzen("scans") == 0     # nur Warteschlange geleert
    assert u["db"].anzahl("import") == 1 and w.status()["import_gesamt"] == 1


def test_link_nach_dem_einreihen_wird_nicht_gelesen(umgebung, tmp_path):
    u = umgebung
    geheim = tmp_path / "daten" / "secret.key"
    geheim.write_bytes(b"GEHEIM")
    q = u["import"] / "a.pdf"
    q.write_bytes(b"harmlos")
    w = _worker(u)
    assert w.import_starten("import") == 1
    q.unlink()
    os.symlink(geheim, q)                            # Datei nach dem Einreihen gegen einen Link getauscht
    w.schritt()
    assert u["db"].zahlen()["abgelegt"] == 0 and u["db"].anzahl() == 0
    assert w.status()["import_fertig"] == 1


def test_verlinkter_elternordner_nach_dem_einreihen(umgebung):
    u = umgebung
    (u["import"] / "Steuer").mkdir()
    (u["import"] / "Steuer" / "a.pdf").write_bytes(b"x")
    w = _worker(u)
    w.import_starten("import")
    (u["archiv"] / "Steuer").mkdir()
    (u["archiv"] / "Steuer" / "a.pdf").write_bytes(b"archiviert")
    (u["import"] / "Steuer" / "a.pdf").unlink()
    (u["import"] / "Steuer").rmdir()
    os.symlink(u["archiv"] / "Steuer", u["import"] / "Steuer")
    w.schritt()
    assert u["db"].zahlen()["abgelegt"] == 0


def test_link_im_eingang_wird_nicht_eingereiht(umgebung, tmp_path):
    u = umgebung
    (tmp_path / "fremd.pdf").write_bytes(b"x")
    os.symlink(tmp_path / "fremd.pdf", u["eingang"] / "link.pdf")
    w = _worker(u)
    _bereit(w, u)
    assert u["db"].anzahl() == 0


def test_zuruecksetzen_waehrend_der_verarbeitung_verwirft_die_arbeit(umgebung):
    u = umgebung
    (u["import"] / "a.pdf").write_bytes(b"x")
    w = None

    def ki_mit_zuruecksetzen(*a, **k):
        w.import_zuruecksetzen("import")             # passiert, während die KI noch rechnet
        return ki.Einordnung("Steuer", "Finanzamt", date(2024, 1, 1), "Bescheid", 0.9)
    w = _worker(u, ki_fn=ki_mit_zuruecksetzen)
    w.import_starten("import")
    w.schritt()
    assert u["db"].zahlen()["abgelegt"] == 0
    assert list((u["daten"] / "arbeit").iterdir()) == []


def test_dubletten_von_frueher_werden_nachgetragen(umgebung):
    u = umgebung
    (u["import"] / "A").mkdir()
    (u["import"] / "B").mkdir()
    for p in (u["import"] / "A" / "x.pdf", u["import"] / "B" / "x-kopie.pdf"):
        p.write_bytes(b"gleich")
    (u["import"] / "einzeln.pdf").write_bytes(b"anders")
    w = _worker(u)
    w.import_starten("import")
    for _ in range(3):
        w.schritt()
    u["db"]._x("DELETE FROM dubletten")
    assert w.dubletten_nachtragen() == 1                   # Original bekannt → nur die Kopie
    (g,) = u["db"].dubletten_gruppen()
    assert [f["name"] for f in g["dateien"]] == ["x-kopie.pdf"]
    # So sah es vor 1.8 aus: nur gezählt, Herkunft der abgelegten Datei unbekannt → ganze Gruppe zeigen.
    u["db"]._x("DELETE FROM dubletten")
    u["db"]._x("UPDATE dokumente SET quelle_datei=''")
    u["db"].setze_zaehler("dubletten_nachgetragen", 0)
    assert w.dubletten_nachtragen() == 2
    (g,) = u["db"].dubletten_gruppen()
    assert sorted(f["name"] for f in g["dateien"]) == ["x-kopie.pdf", "x.pdf"]
    assert all(f["nachgetragen"] for f in g["dateien"])
    assert w.dubletten_nachtragen() == 0                   # nur einmal


def test_erneuter_import_ist_keine_dublette_von_sich_selbst(umgebung):
    u = umgebung
    (u["import"] / "a.pdf").write_bytes(b"eins")
    w = _worker(u)
    w.import_starten("import")
    w.schritt()
    w.import_starten("import")                       # z. B. nach „Import abbrechen“ wieder gestartet
    w.schritt()
    assert u["db"].dubletten_gruppen() == [] and u["db"].zaehler("dubletten") == 0
    assert w.status()["import_fertig"] == 1


def test_abbrechen_waehrend_der_arbeit_verzaehlt_nicht(umgebung):
    u = umgebung
    (u["import"] / "a.pdf").write_bytes(b"eins")
    w = None

    def ki_mit_abbruch(*a, **k):
        w.import_abbrechen("import")
        return ki.Einordnung("Steuer", "FA", date(2024, 1, 1), "Bescheid", 0.9)
    w = _worker(u, ki_fn=ki_mit_abbruch)
    w.import_starten("import")
    w.schritt()
    s = w.status()
    assert (s["import_fertig"], s["import_gesamt"]) == (0, 0)


def test_alte_dubletten_haengen_nicht_an_neuer_id(umgebung):
    u = umgebung
    i = u["db"].einfuegen(text="a", sha256="aaa", rel_pfad="x.pdf", art="Steuer", absender="FA",
                          datum="2024-01-01", titel="A")
    u["db"].dublette_merken("upload", "a.pdf", "Upload", i, "aaa")
    u["db"].loeschen(i)
    j = u["db"].einfuegen(text="b", sha256="bbb", rel_pfad="y.pdf", art="Steuer", absender="FA",
                          datum="2024-01-01", titel="B")
    assert j == i and u["db"].dubletten_zu(j) == 0
    assert u["db"].dubletten_gruppen()[0]["titel"] is None
