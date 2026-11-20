# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import ki
from db import Db
from extrakt import Ergebnis
from verarbeitung import Worker

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    d = {n: tmp_path / n for n in ("eingang", "archiv", "import", "daten")}
    for p in d.values():
        p.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d["daten"])
    monkeypatch.setattr(config, "USERS_FILE", d["daten"] / "users.json")
    monkeypatch.setattr(config, "SECRET_FILE", d["daten"] / "secret.key")
    monkeypatch.setattr(config, "EINSTELLUNGEN_DATEI", d["daten"] / "einstellungen.json")
    monkeypatch.setattr(config, "STANDARD_IMPORT_ORDNER", "import")
    monkeypatch.setitem(config.STANDARD, "import_ordner", "import")
    import auth
    monkeypatch.setattr(auth, "_serializer", None)
    db = Db(d["daten"] / "t.sqlite")

    def ki_fn(text, dateiname, arten, absender_bekannt, url, timeout=300, hinweise=None):
        return ki.Einordnung("Versicherungen", "Techniker Krankenkasse", date(2026, 11, 12),
                             "Beitragsbescheid", 0.95)
    w = Worker(db, config.lade_einstellungen, ki_fn=ki_fn,
               lies_fn=lambda p, a: Ergebnis("Beitragsbescheid Text", Path(p), "pdf"),
               eingang=d["eingang"], archiv=d["archiv"], quellen=tmp_path, daten=d["daten"],
               ki_url="http://ki", ki_ok_fn=lambda: True)
    import server
    app = server.erstelle_app(db=db, worker=w, start_worker=False, archiv=d["archiv"], eingang=d["eingang"])
    server._fehlversuche.clear()
    d.update(db=db, worker=w, client=TestClient(app, follow_redirects=False))
    return d


def _einrichten(c):
    r = c.post("/einrichten", data={"benutzer": "fabian", "passwort": "geheim123"}, headers=ORIGIN)
    assert r.status_code == 303
    r = c.post("/anmelden", data={"benutzer": "fabian", "passwort": "geheim123"}, headers=ORIGIN)
    assert r.status_code == 303 and "ablage_sitzung" in r.cookies
    return c


def _ein_dokument(u):
    (u["eingang"] / "brief.pdf").write_bytes(b"%PDF-1\n%%EOF\n")
    u["db"].einreihen(str(u["eingang"] / "brief.pdf"), "upload")
    assert u["worker"].schritt()
    return u["db"].suche()[0][0]


def test_ohne_admin_zur_einrichtung_dann_anmeldung(umgebung):
    c = umgebung["client"]
    assert c.get("/").headers["location"] == "/einrichten"
    c.post("/einrichten", data={"benutzer": "fabian", "passwort": "geheim123"}, headers=ORIGIN)
    assert c.get("/").headers["location"] == "/anmelden"
    assert c.get("/api/status").status_code == 401


def test_einrichten_nur_einmal(umgebung):
    c = _einrichten(umgebung["client"])
    r = c.post("/einrichten", data={"benutzer": "boese", "passwort": "geheim123"}, headers=ORIGIN)
    assert r.status_code in (303, 403)
    assert "boese" not in config.load_json(config.USERS_FILE, {})["users"]


def test_seiten_laden(umgebung):
    c = _einrichten(umgebung["client"])
    _ein_dokument(umgebung)
    for pfad in ("/", "/suche", "/suche?art=Versicherungen&unsicher=1", "/einstellungen", "/bericht"):
        r = c.get(pfad)
        assert r.status_code == 200, pfad
        assert "FAH-Ablage" in r.text
    assert "ⓘ" in c.get("/einstellungen").text


def test_upload_landet_im_eingang(umgebung):
    c = _einrichten(umgebung["client"])
    r = c.post("/api/upload", files=[("dateien", ("../../böse.pdf", b"%PDF-x", "application/pdf"))],
               headers=ORIGIN)
    assert r.status_code == 200 and r.json()["angenommen"] == 1
    dateien = list(umgebung["eingang"].iterdir())
    assert len(dateien) == 1 and dateien[0].name.endswith(".pdf") and ".." not in dateien[0].name
    assert umgebung["db"].anzahl("upload") == 1


def test_suche_filter(umgebung):
    c = _einrichten(umgebung["client"])
    _ein_dokument(umgebung)
    assert "Beitragsbescheid" in c.get("/suche?q=beitrag").text
    assert "Beitragsbescheid" in c.get("/suche?art=Versicherungen&von=2026-01-01&bis=2026-12-31").text
    assert "Beitragsbescheid" not in c.get("/suche?art=Steuer").text


def test_korrektur_verschiebt_und_lernt(umgebung):
    c = _einrichten(umgebung["client"])
    d = _ein_dokument(umgebung)
    r = c.post(f"/dokument/{d['id']}", data={"art": "Versicherungen", "absender": "TK",
                                             "datum": "2026-11-12", "titel": "Beitragsbescheid"},
               headers=ORIGIN)
    assert r.status_code == 303
    assert (umgebung["archiv"] / "Versicherungen/TK/2026/2026-11-12 Beitragsbescheid.pdf").exists()
    assert not (umgebung["archiv"] / "Versicherungen/Techniker Krankenkasse").exists()
    from regeln import Regeln
    assert Regeln(umgebung["db"]).kanon("Techniker Krankenkasse") == "TK"


def test_korrektur_ungueltige_art_abgelehnt(umgebung):
    c = _einrichten(umgebung["client"])
    d = _ein_dokument(umgebung)
    r = c.post(f"/dokument/{d['id']}", data={"art": "Erfunden", "absender": "TK",
                                             "datum": "2026-11-12", "titel": "X"}, headers=ORIGIN)
    assert r.status_code == 400


def test_fremder_origin_abgelehnt(umgebung):
    c = _einrichten(umgebung["client"])
    r = c.post("/api/upload", files=[("dateien", ("a.pdf", b"x", "application/pdf"))],
               headers={"Origin": "http://boese.example"})
    assert r.status_code == 403
    r = c.post("/api/upload", files=[("dateien", ("a.pdf", b"x", "application/pdf"))])
    assert r.status_code == 403


def test_datei_nur_per_id(umgebung):
    c = _einrichten(umgebung["client"])
    d = _ein_dokument(umgebung)
    r = c.get(f"/datei/{d['id']}")
    assert r.status_code == 200 and r.content == b"%PDF-1\n%%EOF\n"
    assert c.get("/datei/9999").status_code == 404


def test_papierkorb_und_zurueck(umgebung):
    c = _einrichten(umgebung["client"])
    d = _ein_dokument(umgebung)
    assert c.post(f"/dokument/{d['id']}/papierkorb", headers=ORIGIN).status_code == 303
    assert "Beitragsbescheid" not in c.get("/suche").text
    assert "Beitragsbescheid" in c.get("/suche?papierkorb=1").text
    assert c.post(f"/dokument/{d['id']}/wiederherstellen", headers=ORIGIN).status_code == 303
    assert "Beitragsbescheid" in c.get("/suche").text


def test_einstellungen_speichern(umgebung):
    c = _einrichten(umgebung["client"])
    vorher = config.lade_einstellungen()["arten"]
    r = c.post("/einstellungen", data={"unsicher_schwelle": "0.6"}, headers=ORIGIN)
    assert r.status_code == 303
    e = config.lade_einstellungen()
    assert e["arten"] == vorher and e["unsicher_schwelle"] == 0.6
    assert 'href="/arten"' in c.get("/einstellungen").text


def test_import_starten(umgebung):
    c = _einrichten(umgebung["client"])
    (umgebung["import"] / "a.pdf").write_bytes(b"x")
    assert c.post("/import", headers=ORIGIN).status_code == 303
    assert c.get("/api/status").json()["import_gesamt"] == 1


def test_anmeldebremse(umgebung):
    c = umgebung["client"]
    c.post("/einrichten", data={"benutzer": "fabian", "passwort": "geheim123"}, headers=ORIGIN)
    for _ in range(5):
        assert c.post("/anmelden", data={"benutzer": "fabian", "passwort": "falsch"}, headers=ORIGIN).status_code == 401
    r = c.post("/anmelden", data={"benutzer": "fabian", "passwort": "geheim123"}, headers=ORIGIN)
    assert r.status_code == 429


# ── Befunde der Prüfung ─────────────────────────────────────────────────
def _abgelegt_mit_endung(u, name, inhalt):
    (u["eingang"] / name).write_bytes(inhalt)
    u["db"].einreihen(str(u["eingang"] / name), "upload")
    assert u["worker"].schritt()
    return u["db"].suche(sortierung="eingang")[0][0]


def test_html_datei_nur_als_download(umgebung):
    c = _einrichten(umgebung["client"])
    umgebung["worker"].lies_fn = lambda p, a: Ergebnis("x", Path(p), Path(p).suffix.lstrip("."))
    d = _abgelegt_mit_endung(umgebung, "rechnung.html", b"<script>alert(1)</script>")
    r = c.get(f"/datei/{d['id']}")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in r.headers["content-security-policy"]


def test_pdf_inline_mit_typ(umgebung):
    c = _einrichten(umgebung["client"])
    d = _ein_dokument(umgebung)
    r = c.get(f"/datei/{d['id']}")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")


def test_seiten_mit_csp(umgebung):
    c = _einrichten(umgebung["client"])
    csp = c.get("/").headers["content-security-policy"]
    assert "default-src 'self'" in csp and "object-src 'none'" in csp


def test_kein_zugriffsprotokoll():
    text = (Path(__file__).resolve().parent.parent / "docker-entrypoint.sh").read_text()
    assert "--no-access-log" in text and "umask 027" in text


def test_fremder_hostname_abgelehnt(umgebung):
    c = umgebung["client"]
    r = c.get("/einrichten", headers={"Host": "boese.example:8093"})
    assert r.status_code == 400
    r = c.post("/einrichten", data={"benutzer": "x", "passwort": "geheim123"},
               headers={"Host": "boese.example:8093", "Origin": "http://boese.example:8093"})
    assert r.status_code == 400 and not config.USERS_FILE.exists()
    assert c.get("/einrichten", headers={"Host": "192.168.1.23:8093"}).status_code == 200


def test_kaputte_zugangsdatei_oeffnet_keine_einrichtung(umgebung):
    c = _einrichten(umgebung["client"])
    config.USERS_FILE.write_text("{kaputt")
    assert c.get("/").status_code == 503
    r = c.post("/einrichten", data={"benutzer": "boese", "passwort": "geheim123"}, headers=ORIGIN)
    assert r.status_code in (403, 503)
    assert config.USERS_FILE.read_text() == "{kaputt"


def test_korrektur_wenn_datei_weg(umgebung):
    c = _einrichten(umgebung["client"])
    d = _ein_dokument(umgebung)
    (umgebung["archiv"] / d["rel_pfad"]).unlink()
    r = c.post(f"/dokument/{d['id']}", data={"art": "Steuer", "absender": "TK", "datum": "2026-11-12",
                                             "titel": "X"}, headers=ORIGIN)
    assert r.status_code == 409 and "nicht mehr" in r.text


def test_langer_uploadname_behaelt_endung(umgebung):
    c = _einrichten(umgebung["client"])
    name = "Kontoauszug_" + "X" * 100 + ".pdf"
    c.post("/api/upload", files=[("dateien", (name, b"%PDF-x", "application/pdf"))], headers=ORIGIN)
    (datei,) = list(umgebung["eingang"].iterdir())
    assert datei.suffix == ".pdf" and len(datei.name) <= 110


def test_bestaetigen_ohne_aenderung_benennt_nicht_um(umgebung):
    c = _einrichten(umgebung["client"])
    _ein_dokument(umgebung)
    (umgebung["eingang"] / "zwei.pdf").write_bytes(b"%PDF-2\n%%EOF\n")
    umgebung["db"].einreihen(str(umgebung["eingang"] / "zwei.pdf"), "upload")
    umgebung["worker"].schritt()
    d2 = umgebung["db"].suche(sortierung="eingang")[0][0]
    assert d2["rel_pfad"].endswith("(2).pdf")
    c.post(f"/dokument/{d2['id']}", data={"art": d2["art"], "absender": d2["absender"], "datum": d2["datum"],
                                          "titel": d2["titel"]}, headers=ORIGIN)
    assert umgebung["db"].hole(d2["id"])["rel_pfad"] == d2["rel_pfad"]
    from regeln import Regeln
    assert Regeln(umgebung["db"]).art_fuer(d2["absender"]) is None   # nur bestätigt → keine Regel


def test_uvicorn_fehler_ohne_inhalt():
    import logging
    import server
    try:
        raise FileNotFoundError("/archiv/Versicherungen/Geheimabsender/2026/x.pdf")
    except FileNotFoundError:
        import sys
        rec = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
                                None, sys.exc_info())
    server._OhneInhalt().filter(rec)
    text = logging.Formatter().format(rec)
    assert "Geheimabsender" not in text and "FileNotFoundError" in text


def test_import_zuruecksetzen_route(umgebung):
    c = _einrichten(umgebung["client"])
    (umgebung["import"] / "a.pdf").write_bytes(b"%PDF-1\n%%EOF\n")
    c.post("/import", headers=ORIGIN)
    umgebung["worker"].schritt()
    assert umgebung["db"].zahlen()["abgelegt"] == 1
    assert c.post("/import/zuruecksetzen", headers=ORIGIN).status_code == 303
    assert umgebung["db"].zahlen()["abgelegt"] == 0


# ── Paperless-ähnliche Ansicht ──────────────────────────────────────────
def _drei_dokumente(u):
    for i, (art, ab, titel) in enumerate([("Versicherungen", "TK", "Beitragsbescheid"),
                                          ("Steuer", "Finanzamt Kelheim", "Anpassung"),
                                          ("Versicherungen", "HUK", "Zulassung")]):
        rel = f"{art}/{ab}/2026/2026-0{i + 1}-01 {titel}.pdf"
        (u["archiv"] / rel).parent.mkdir(parents=True, exist_ok=True)
        (u["archiv"] / rel).write_bytes(b"%PDF-1\n%%EOF\n")
        u["db"].einfuegen(sha256=f"s{i}", rel_pfad=rel, art=art, absender=ab, datum=f"2026-0{i + 1}-01",
                          titel=titel, unsicher=0, quelle="eingang", text=titel)


def test_dokumente_tabelle_sortierbar(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    r = c.get("/dokumente?ansicht=tabelle&sortierung=titel&richtung=auf")
    assert r.status_code == 200 and "<table" in r.text
    assert r.text.index("Anpassung") < r.text.index("Beitragsbescheid") < r.text.index("Zulassung")
    r = c.get("/dokumente?ansicht=tabelle&sortierung=titel&richtung=ab")
    assert r.text.index("Zulassung") < r.text.index("Anpassung")


def test_dokumente_kacheln_mit_vorschau(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    r = c.get("/dokumente?ansicht=kacheln&art=Versicherungen")
    ids = [d["id"] for d in umgebung["db"].suche(art="Versicherungen")[0]]
    for i in ids:
        assert f'/vorschau/{i}' in r.text
    assert "Anpassung" not in r.text
    assert c.get("/suche?art=Steuer").status_code == 200       # alter Pfad geht weiter


def test_absender_und_arten_seiten(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    r = c.get("/absender")
    assert r.status_code == 200 and "Finanzamt Kelheim" in r.text and "/dokumente?absender=Finanzamt%20Kelheim" in r.text
    r = c.get("/arten")
    assert r.status_code == 200 and "Versicherungen" in r.text and "2 Dokumente" in r.text


def test_vorschau_route(umgebung, monkeypatch):
    import vorschau
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    d = umgebung["db"].suche()[0][0]

    def fake(args, timeout=120):
        Path(args[-1] + ".png").write_bytes(b"\x89PNG-x")
        import subprocess
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(vorschau, "_lauf", fake)
    r = c.get(f"/vorschau/{d['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == b"\x89PNG-x"
    assert c.get("/vorschau/9999").status_code == 404
    assert TestClient(c.app).get(f"/vorschau/{d['id']}").status_code == 401   # ohne Anmeldung



def test_csv_vorschau_und_tabelle(umgebung):
    import extrakt
    c = _einrichten(umgebung["client"])
    umgebung["worker"].lies_fn = extrakt.lies                 # echte CSV-Lesung statt PDF-Attrappe
    (umgebung["eingang"] / "liste.csv").write_text("Datum;Betrag;Notiz\n01.02.2024;12,50;<b>fett</b>\n", encoding="utf-8")
    umgebung["db"].einreihen(str(umgebung["eingang"] / "liste.csv"), "upload")
    assert umgebung["worker"].schritt()
    d = umgebung["db"].suche()[0][0]
    r = c.get(f"/vorschau/{d['id']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"] and "Datum;Betrag;Notiz" in r.text
    assert "<b>" not in r.text
    r = c.get(f"/dokument/{d['id']}")
    assert r.status_code == 200 and "textvorschau-tabelle" in r.text
    assert "<th>Betrag</th>" in r.text and "<td>12,50</td>" in r.text
    assert "&lt;b&gt;fett&lt;/b&gt;" in r.text and "<b>fett</b>" not in r.text
    assert "keine Vorschau" not in r.text


def test_xls_text_wird_nachgeholt_und_platzhalter_ersetzt(umgebung):
    xlwt = pytest.importorskip("xlwt")
    import vorschau
    c = _einrichten(umgebung["client"])
    wb = xlwt.Workbook()
    ws = wb.add_sheet("A")
    ws.write(0, 0, "Monat"); ws.write(0, 1, "Stunden"); ws.write(1, 0, "Dezember"); ws.write(1, 1, 151.5)
    wb.save(str(umgebung["eingang"] / "zeiten.xls"))
    # so wie vor 1.4.2 abgelegt: ohne Text
    umgebung["worker"].lies_fn = lambda p, a: Ergebnis("", Path(p), "xls")
    umgebung["db"].einreihen(str(umgebung["eingang"] / "zeiten.xls"), "upload")
    assert umgebung["worker"].schritt()
    d = umgebung["db"].suche()[0][0]
    assert d["rel_pfad"].endswith(".xls") and umgebung["db"].text(d["id"]) == ""
    ordner = config.DATA_DIR / "vorschau"
    ordner.mkdir(exist_ok=True)
    (ordner / f"{d['id']}.svg").write_text(vorschau.blatt_svg("", "xls"))     # Platzhalter aus 1.4.1
    r = c.get(f"/vorschau/{d['id']}")
    assert r.status_code == 200 and "Dezember" in r.text and "Kein Text gefunden" not in r.text
    assert "Dezember" in umgebung["db"].text(d["id"])                          # auch für die Suche
    r = c.get(f"/dokument/{d['id']}")
    assert "<th>Monat</th>" in r.text and "<td>151.5</td>" in r.text


def test_platzhalter_bleibt_ohne_text(umgebung, monkeypatch):
    import extrakt
    import vorschau
    c = _einrichten(umgebung["client"])
    (umgebung["eingang"] / "leer.txt").write_text("   ")
    umgebung["worker"].lies_fn = lambda p, a: Ergebnis("", Path(p), "txt")
    umgebung["db"].einreihen(str(umgebung["eingang"] / "leer.txt"), "upload")
    assert umgebung["worker"].schritt()
    d = umgebung["db"].suche()[0][0]
    gelesen = []
    echt = extrakt.lies
    monkeypatch.setattr(extrakt, "lies", lambda p, a: gelesen.append(p) or echt(p, a))
    r = c.get(f"/vorschau/{d['id']}")
    assert r.status_code == 200 and vorschau.PLATZHALTER in r.text and r.headers["cache-control"] == "no-cache"
    assert c.get(f"/vorschau/{d['id']}?v=1.5.0").text == r.text
    assert "kein Text gefunden" in c.get(f"/dokument/{d['id']}").text
    assert len(gelesen) == 1                                   # Leseversuch gemerkt, nicht jedes Mal neu
    monkeypatch.setattr(extrakt, "LESER_VERSION", extrakt.LESER_VERSION + 1)
    c.get(f"/dokument/{d['id']}")
    assert len(gelesen) == 2                                   # neue Leser-Version → einmal neu versuchen

# ── Prüfmodus: nach dem Speichern gleich das nächste unsichere Dokument ──
def _unsichere(u, n=3):
    ids = []
    for i in range(n):
        rel = f"Arbeit & Gehalt/BA/2024/2024-12-0{i + 1} Bescheinigung.pdf"
        (u["archiv"] / rel).parent.mkdir(parents=True, exist_ok=True)
        (u["archiv"] / rel).write_bytes(b"%PDF-1\n%%EOF\n")
        ids.append(u["db"].einfuegen(sha256=f"u{i}", rel_pfad=rel, art="Arbeit & Gehalt", absender="BA",
                                     datum=f"2024-12-0{i + 1}", titel="Bescheinigung", unsicher=1,
                                     grund="kein Datum erkannt", quelle="import", text="x"))
    return ids   # Listenreihenfolge (Datum absteigend): ids[2], ids[1], ids[0]


def test_unsicher_liste_verlinkt_pruefmodus(umgebung):
    c = _einrichten(umgebung["client"])
    ids = _unsichere(umgebung)
    r = c.get("/dokumente?unsicher=1")
    assert f"/dokument/{ids[2]}?pruefen=1" in r.text


def test_pruefen_speichern_springt_zum_naechsten(umgebung):
    c = _einrichten(umgebung["client"])
    ids = _unsichere(umgebung)
    daten = {"art": "Arbeit & Gehalt", "absender": "BA", "titel": "Bescheinigung", "pruefen": "1"}
    r = c.post(f"/dokument/{ids[2]}", data={**daten, "datum": "2024-12-03"}, headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/dokument/{ids[1]}?pruefen=1")
    r = c.post(f"/dokument/{ids[1]}", data={**daten, "datum": "2024-12-02"}, headers=ORIGIN)
    assert r.headers["location"].startswith(f"/dokument/{ids[0]}?pruefen=1")
    r = c.post(f"/dokument/{ids[0]}", data={**daten, "datum": "2024-12-01"}, headers=ORIGIN)
    assert r.headers["location"] == "/dokumente?unsicher=1&fertig=1"
    assert "Alles geprüft" in c.get(r.headers["location"]).text


def test_pruefleiste_mit_ueberspringen(umgebung):
    c = _einrichten(umgebung["client"])
    ids = _unsichere(umgebung)
    r = c.get(f"/dokument/{ids[2]}?pruefen=1")
    assert "noch 3" in r.text and f'href="/dokument/{ids[1]}?pruefen=1"' in r.text
    assert 'name="pruefen" value="1"' in r.text
    r = c.get(f"/dokument/{ids[0]}?pruefen=1")             # letztes → wieder von vorn
    assert f'href="/dokument/{ids[2]}?pruefen=1"' in r.text


def test_pruefen_papierkorb_springt_weiter(umgebung):
    c = _einrichten(umgebung["client"])
    ids = _unsichere(umgebung)
    r = c.post(f"/dokument/{ids[2]}/papierkorb", data={"pruefen": "1"}, headers=ORIGIN)
    assert r.headers["location"].startswith(f"/dokument/{ids[1]}?pruefen=1")


# ── Sicherung & Wiederherstellung in der Oberfläche ─────────────────────
def _warten(c):
    t = getattr(c.app.state, "sicherung_thread", None)
    if t:
        t.join(10)


def test_sicherung_erstellen_und_herunterladen(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    assert c.post("/sicherung/erstellen", headers=ORIGIN).status_code == 303
    _warten(c)
    import sicherung
    (eintrag,) = sicherung.liste(config.DATA_DIR / "sicherungen")
    r = c.get(f"/sicherung/datei/{eintrag['name']}")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    assert c.get("/sicherung/datei/..%2Fablage.sqlite").status_code == 404
    assert eintrag["name"] in c.get("/einstellungen").text


def test_wiederherstellen_nur_leer_und_dann_vollstaendig(umgebung, tmp_path, monkeypatch):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    c.post("/sicherung/erstellen", headers=ORIGIN)
    _warten(c)
    import sicherung
    name = sicherung.liste(config.DATA_DIR / "sicherungen")[0]["name"]
    r = c.post("/sicherung/wiederherstellen", data={"name": name}, headers=ORIGIN)
    assert r.status_code == 409 and "leer" in r.text
    tar = (config.DATA_DIR / "sicherungen" / name).read_bytes()

    # frische Installation: neue Ordner, leere Datenbank, gleicher Zugang
    import server
    neu = {n: tmp_path / "neu" / n for n in ("eingang", "archiv", "daten")}
    for p in neu.values():
        p.mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", neu["daten"])
    db2 = Db(neu["daten"] / "t.sqlite")
    w2 = Worker(db2, config.lade_einstellungen, eingang=neu["eingang"], archiv=neu["archiv"],
                quellen=tmp_path / "neu", daten=neu["daten"], ki_url="http://ki", ki_ok_fn=lambda: True)
    c2 = TestClient(server.erstelle_app(db=db2, worker=w2, start_worker=False, archiv=neu["archiv"],
                                        eingang=neu["eingang"]), follow_redirects=False)
    c2.cookies.update(c.cookies)
    r = c2.post("/sicherung/hochladen", files=[("datei", ("irgendwas.tar", tar, "application/x-tar"))], headers=ORIGIN)
    assert r.status_code == 303
    (hoch,) = sicherung.liste(neu["daten"] / "sicherungen")
    assert c2.post("/sicherung/wiederherstellen", data={"name": hoch["name"]}, headers=ORIGIN).status_code == 303
    _warten(c2)
    assert db2.zahlen()["abgelegt"] == 3
    assert (neu["archiv"] / "Versicherungen/TK/2026/2026-01-01 Beitragsbescheid.pdf").exists()
    assert "Wiederhergestellt" in c2.get("/einstellungen").text


# ── Arten verwalten ─────────────────────────────────────────────────────
def test_art_anlegen_mit_hinweis(umgebung):
    c = _einrichten(umgebung["client"])
    r = c.post("/arten/neu", data={"name": "Haustiere", "hinweis": "Tierarzt, Hundesteuer"}, headers=ORIGIN)
    assert r.status_code == 303
    e = config.lade_einstellungen()
    assert e["arten"][-2:] == ["Haustiere", "Sonstiges"]
    assert e["art_hinweise"]["Haustiere"] == "Tierarzt, Hundesteuer"
    assert "Haustiere" in c.get("/arten").text
    assert c.post("/arten/neu", data={"name": "haustiere", "hinweis": ""}, headers=ORIGIN).status_code == 409


def test_art_loeschen_nur_ohne_dokumente(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    r = c.post("/arten/loeschen", data={"name": "Versicherungen"}, headers=ORIGIN)
    assert r.status_code == 409 and "Versicherungen" in config.lade_einstellungen()["arten"]
    assert c.post("/arten/loeschen", data={"name": "Wohnen"}, headers=ORIGIN).status_code == 303
    assert "Wohnen" not in config.lade_einstellungen()["arten"]
    assert c.post("/arten/loeschen", data={"name": "Sonstiges"}, headers=ORIGIN).status_code == 409


def test_art_umbenennen_route(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    r = c.post("/arten/umbenennen", data={"alt": "Steuer", "neu": "Steuern", "hinweis": "Finanzamt"}, headers=ORIGIN)
    assert r.status_code == 303
    e = config.lade_einstellungen()
    assert "Steuern" in e["arten"] and "Steuer" not in e["arten"] and e["art_hinweise"]["Steuern"] == "Finanzamt"
    assert umgebung["db"].suche(art="Steuern")[1] == 1
    assert (umgebung["archiv"] / "Steuern").is_dir()


# ── Mehrfachauswahl ─────────────────────────────────────────────────────
def _ids(u):
    return {d["titel"]: d["id"] for d in u["db"].suche()[0]}


def test_liste_hat_auswahl_uebersicht_nicht(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    for ansicht in ("tabelle", "kacheln"):
        r = c.get(f"/dokumente?ansicht={ansicht}")
        assert r.text.count('form="sammel"') == 3 and 'id="sammel"' in r.text and "data-alle-waehlen" in r.text
    assert 'form="sammel"' not in c.get("/").text


def test_sammel_speichern_setzt_nur_ausgefuellte_felder(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    db = umgebung["db"]
    i = _ids(umgebung)
    db.aendern(i["Beitragsbescheid"], unsicher=1, grund="x")
    db.aendern(i["Zulassung"], unsicher=1, grund="x")
    r = c.post("/sammel", data={"ids": [i["Beitragsbescheid"], i["Zulassung"]], "aktion": "speichern",
                                "art": "Steuer", "absender": "", "datum": "",
                                "zurueck": "/dokumente?ansicht=kacheln&erledigt=9"}, headers=ORIGIN)
    assert r.status_code == 303
    assert r.headers["location"] == "/dokumente?ansicht=kacheln&erledigt=2&was=speichern&fehlgeschlagen=0"
    a, b, z = db.hole(i["Beitragsbescheid"]), db.hole(i["Anpassung"]), db.hole(i["Zulassung"])
    assert (a["art"], a["absender"], a["datum"], a["unsicher"]) == ("Steuer", "TK", "2026-01-01", 0)
    assert (z["art"], z["absender"], z["datum"], z["unsicher"]) == ("Steuer", "HUK", "2026-03-01", 0)
    assert (umgebung["archiv"] / a["rel_pfad"]).is_file() and a["rel_pfad"].startswith("Steuer/TK/")
    assert db._c.execute("SELECT count(*) FROM absender_art").fetchone()[0] == 0     # Sammel lernt keine Regeln
    assert db._c.execute("SELECT count(*) FROM aliase").fetchone()[0] == 0
    assert b["art"] == "Steuer" and b["rel_pfad"].startswith("Steuer/Finanzamt")      # nicht angefasst
    # nur Absender + Datum, Art bleibt
    c.post("/sammel", data={"ids": [i["Anpassung"]], "aktion": "speichern", "absender": "  Finanzamt  Landshut ",
                            "datum": "2025-05-05"}, headers=ORIGIN)
    b = db.hole(i["Anpassung"])
    assert (b["art"], b["absender"], b["datum"]) == ("Steuer", "Finanzamt Landshut", "2025-05-05")
    assert "2 Dokumente\n  gespeichert" in c.get(r.headers["location"]).text


def test_sammel_lehnt_falsches_ab(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    i = _ids(umgebung)
    assert c.post("/sammel", data={"ids": [i["Anpassung"]], "aktion": "speichern", "art": "Gibtsnicht"},
                  headers=ORIGIN).status_code == 400
    assert c.post("/sammel", data={"ids": [i["Anpassung"]], "aktion": "speichern", "datum": "31.02.2024"},
                  headers=ORIGIN).status_code == 400
    assert umgebung["db"].hole(i["Anpassung"])["art"] == "Steuer"
    for boese in ("https://boese.example/x", "//boese.example/dokumente", "/einstellungen", "/dokumente/../x"):
        r = c.post("/sammel", data={"ids": [i["Anpassung"]], "aktion": "nix", "zurueck": boese}, headers=ORIGIN)
        assert r.headers["location"] == "/dokumente"
    assert c.post("/sammel", data={"ids": [i["Anpassung"]], "aktion": "papierkorb"}).status_code == 403  # fremde Seite
    r = TestClient(c.app, follow_redirects=False).post("/sammel", data={"ids": [1], "aktion": "papierkorb"},
                                                       headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/anmelden"
    assert umgebung["db"].hole(i["Anpassung"])["status"] == "abgelegt"


def test_sammel_papierkorb_und_zurueck(umgebung):
    c = _einrichten(umgebung["client"])
    _drei_dokumente(umgebung)
    db = umgebung["db"]
    i = _ids(umgebung)
    auswahl = [i["Beitragsbescheid"], i["Zulassung"], 9999]
    r = c.post("/sammel", data={"ids": auswahl, "aktion": "papierkorb", "zurueck": "/dokumente?seite_nr=3"},
               headers=ORIGIN)
    assert r.headers["location"] == "/dokumente?erledigt=2&was=papierkorb&fehlgeschlagen=1"
    assert db.hole(i["Zulassung"])["status"] == "papierkorb" and db.hole(i["Anpassung"])["status"] == "abgelegt"
    seite = c.get("/dokumente?papierkorb=1").text
    assert 'value="wiederherstellen"' in seite and 'name="art"' not in seite.split('id="sammel"')[1]
    r = c.post("/sammel", data={"ids": auswahl[:2], "aktion": "wiederherstellen", "zurueck": "/dokumente?papierkorb=1"},
               headers=ORIGIN)
    assert r.headers["location"] == "/dokumente?papierkorb=1&erledigt=2&was=wiederherstellen&fehlgeschlagen=0"
    assert all(db.hole(x)["status"] == "abgelegt" for x in auswahl[:2])
    assert (umgebung["archiv"] / db.hole(i["Zulassung"])["rel_pfad"]).is_file()


def test_ordnerwahl_api_und_speichern(umgebung, tmp_path):
    c = _einrichten(umgebung["client"])
    (tmp_path / "Scans" / "2019").mkdir(parents=True)
    (tmp_path / "Scans" / "2019" / "a.pdf").write_bytes(b"x")
    j = c.get("/api/ordner").json()
    namen = [o["name"] for o in j["ordner"]]
    assert "Scans" in namen and "import" in namen
    assert not {"archiv", "eingang", "daten"} & set(namen)
    j = c.get("/api/ordner", params={"pfad": "Scans"}).json()
    assert j["dateien"] == 1 and j["ordner"] == [{"name": "2019", "pfad": "Scans/2019"}]
    assert j["teile"] == [{"name": "Scans", "pfad": "Scans"}]
    assert c.get("/api/ordner", params={"pfad": "../"}).status_code == 404
    assert c.get("/api/ordner", params={"pfad": "archiv"}).status_code == 404
    assert c.post("/import/ordner", data={"pfad": "archiv"}, headers=ORIGIN).status_code == 400
    assert c.post("/import/ordner", data={"pfad": "Scans"}, headers=ORIGIN).status_code == 303
    assert config.lade_einstellungen()["import_ordner"] == "Scans"
    seite = c.get("/einstellungen").text
    assert "Scans" in seite and "1 Datei " in seite
    assert c.post("/import", headers=ORIGIN).status_code == 303
    assert umgebung["db"].anzahl("import") == 1


def test_ordnerwahl_api_braucht_anmeldung(umgebung):
    _einrichten(umgebung["client"])
    umgebung["client"].cookies.clear()
    assert umgebung["client"].get("/api/ordner").status_code == 401


def test_zweiter_blick_liste_kachel_und_wiederholen(umgebung):
    c = _einrichten(umgebung["client"])
    db = umgebung["db"]
    i = db.einfuegen(text="x", sha256="b1", rel_pfad="Steuer/FA/2024/a.pdf", art="Steuer", absender="FA",
                     datum="2024-01-01", titel="Bescheid", unsicher=0, blick_geklaert=1,
                     blick_text="Zweiter Blick: eingeordnet wie 3 fast gleiche, schon sichere Dokumente")
    db.einfuegen(text="y", sha256="b2", rel_pfad="Steuer/FA/2024/b.pdf", art="Steuer", absender="FA",
                 datum="2024-01-02", titel="Anderes", unsicher=1, grund="KI unsicher", blick=1)
    seite = c.get("/dokumente", params={"blick": "1"}).text
    assert "Vom zweiten Blick geklärt" in seite and "Bescheid" in seite and "Anderes" not in seite
    assert "zweiter Blick" in c.get("/").text
    assert "eingeordnet wie 3 fast gleiche" in c.get(f"/dokument/{i}").text
    assert c.get("/api/status").json()["blick_geklaert"] == 1
    assert c.post("/blick/wiederholen", headers=ORIGIN).status_code == 303
    assert db.blick_zahlen()["offen"] == 1


def test_doppelte_liste_herunterladen_loeschen(umgebung):
    c = _einrichten(umgebung["client"])
    w, db = umgebung["worker"], umgebung["db"]
    (umgebung["eingang"] / "brief.pdf").write_bytes(b"%PDF-1\n%%EOF\n")
    db.einreihen(str(umgebung["eingang"] / "brief.pdf"), "upload")
    w.schritt()
    (umgebung["eingang"] / "brief (2).pdf").write_bytes(b"%PDF-1\n%%EOF\n")
    db.einreihen(str(umgebung["eingang"] / "brief (2).pdf"), "upload")
    w.schritt()
    (g,) = db.dubletten_gruppen()
    (f,) = g["dateien"]
    assert f["name"] == "brief (2).pdf" and g["quelle_datei"].endswith("/brief.pdf")
    seite = c.get("/doppelte").text
    assert "brief (2).pdf" in seite and "Byte für Byte" in seite and "Herunterladen" in seite
    assert "1 byte-gleiche Datei" in c.get(f"/dokument/{g['dok_id']}").text
    r = c.get(f"/doppelte/{f['id']}/datei")
    assert r.status_code == 200 and r.content == b"%PDF-1\n%%EOF\n"
    assert c.post(f"/doppelte/{f['id']}/loeschen", headers=ORIGIN).status_code == 303
    assert db.dubletten_gruppen() == [] and not Path(f["datei"]).exists()
    assert c.get("/doppelte/999/datei").status_code == 404


def test_doppelte_datei_nur_aus_dem_dubletten_ordner(umgebung):
    c = _einrichten(umgebung["client"])
    geheim = umgebung["daten"] / "users.json"
    umgebung["db"].dublette_merken("eingang", "x.pdf", "Eingang", 1, "abc", str(geheim))
    (e,) = umgebung["db"].dubletten_gruppen()[0]["dateien"]
    assert c.get(f"/doppelte/{e['id']}/datei").status_code == 404
    c.post(f"/doppelte/{e['id']}/loeschen", headers=ORIGIN)
    assert geheim.exists()


def test_warteschlange_und_import_abbrechen(umgebung, tmp_path):
    c = _einrichten(umgebung["client"])
    for n in ("a.pdf", "b.pdf"):
        (umgebung["import"] / n).write_bytes(n.encode())
    c.post("/import", headers=ORIGIN)
    (umgebung["eingang"] / "neu.pdf").write_bytes(b"x")
    umgebung["db"].einreihen(str(umgebung["eingang"] / "neu.pdf"), "upload")
    seite = c.get("/warteschlange").text
    assert "3 Dateien warten" in seite and seite.index("neu.pdf") < seite.index("a.pdf")   # Upload vor Import
    assert "Import abbrechen" in seite
    assert c.post("/warteschlange/import-abbrechen", data={"ordner": "import"}, headers=ORIGIN).status_code == 303
    assert umgebung["db"].anzahl("import") == 0 and umgebung["db"].anzahl() == 1
    assert c.get("/api/status").json()["import_gesamt"] == 0


def test_papierkorb_leeren_und_endgueltig(umgebung):
    c = _einrichten(umgebung["client"])
    w, db = umgebung["worker"], umgebung["db"]
    ids = []
    for n in ("a", "b", "c"):
        (umgebung["eingang"] / f"{n}.pdf").write_bytes(f"%PDF-{n}\n%%EOF\n".encode())
        db.einreihen(str(umgebung["eingang"] / f"{n}.pdf"), "upload")
        w.schritt()
    ids = [d["id"] for d in db.suche()[0]]
    for i in ids:
        w.in_papierkorb(i)
    korb = [Path(db.hole(i)["papierkorb_pfad"]) for i in ids]
    assert "Papierkorb leeren (3)" in c.get("/dokumente", params={"papierkorb": "1"}).text
    # einzeln
    assert c.post(f"/dokument/{ids[0]}/endgueltig", headers=ORIGIN).status_code == 303
    assert db.hole(ids[0]) is None and not korb[0].exists()
    # ausgewählt
    c.post("/sammel", data={"ids": [ids[1]], "aktion": "endgueltig", "zurueck": "/dokumente?papierkorb=1"}, headers=ORIGIN)
    assert db.hole(ids[1]) is None and not korb[1].exists()
    # alles
    r = c.post("/papierkorb/leeren", headers=ORIGIN)
    assert r.status_code == 303 and "erledigt=1" in r.headers["location"]
    assert db.hole(ids[2]) is None and not korb[2].exists()
    assert "1 Dokument" in c.get(r.headers["location"]).text


def test_endgueltig_nur_aus_dem_papierkorb(umgebung):
    c = _einrichten(umgebung["client"])
    w, db = umgebung["worker"], umgebung["db"]
    (umgebung["eingang"] / "a.pdf").write_bytes(b"%PDF-a\n%%EOF\n")
    db.einreihen(str(umgebung["eingang"] / "a.pdf"), "upload")
    w.schritt()
    (d,) = db.suche()[0]
    c.post(f"/dokument/{d['id']}/endgueltig", headers=ORIGIN)
    c.post("/papierkorb/leeren", headers=ORIGIN)
    assert db.hole(d["id"]) is not None and (umgebung["archiv"] / d["rel_pfad"]).exists()
