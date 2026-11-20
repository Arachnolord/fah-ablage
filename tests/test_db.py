# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
from db import Db


def _db(tmp_path):
    d = Db(tmp_path / "t.sqlite")
    d.einfuegen(sha256="a", rel_pfad="Versicherungen/TK/2026/2026-11-12 Beitragsbescheid.pdf",
                art="Versicherungen", absender="TK", datum="2026-11-12", titel="Beitragsbescheid",
                unsicher=0, quelle="eingang", text="Ihr neuer Beitragsbescheid für 2027")
    d.einfuegen(sha256="b", rel_pfad="Steuer/Finanzamt Kelheim/2025/2025-03-01 Steuerbescheid.pdf",
                art="Steuer", absender="Finanzamt Kelheim", datum="2025-03-01", titel="Steuerbescheid",
                unsicher=1, quelle="import", herkunft="Steuer/2024", text="Einkommensteuer 2024")
    d.einfuegen(sha256="c", rel_pfad="Versicherungen/HUK/2024/2024-06-30 Police.pdf",
                art="Versicherungen", absender="HUK", datum="2024-06-30", titel="Police",
                unsicher=0, quelle="upload", text="Kfz-Haftpflicht")
    return d


def test_volltext_praefix(tmp_path):
    d = _db(tmp_path)
    treffer, n = d.suche(text="beitrag")
    assert n == 1 and treffer[0]["absender"] == "TK"


def test_volltext_sonderzeichen_kein_fehler(tmp_path):
    d = _db(tmp_path)
    treffer, n = d.suche(text='beitrag" ( * :')
    assert n == 1


def test_filter_art_und_zeitraum(tmp_path):
    d = _db(tmp_path)
    treffer, n = d.suche(art="Versicherungen", von="2025-01-01", bis="2026-12-31")
    assert n == 1 and treffer[0]["absender"] == "TK"
    treffer, n = d.suche(art="Versicherungen")
    assert n == 2 and treffer[0]["datum"] == "2026-11-12"   # neueste zuerst


def test_nur_unsicher_und_absender(tmp_path):
    d = _db(tmp_path)
    assert d.suche(nur_unsicher=True)[1] == 1
    assert d.suche(absender="HUK")[1] == 1
    assert d.absender_liste("Versicherungen") == ["HUK", "TK"]


def test_papierkorb_nicht_in_suche(tmp_path):
    d = _db(tmp_path)
    i = d.gibt_sha("a")
    d.aendern(i, status="papierkorb")
    assert d.suche(text="beitrag")[1] == 0
    assert d.zahlen()["abgelegt"] == 2


def test_aendern_aktualisiert_volltext(tmp_path):
    d = _db(tmp_path)
    i = d.gibt_sha("c")
    d.aendern(i, absender="HUK-Coburg", titel="Versicherungsschein")
    assert d.suche(text="coburg")[1] == 1
    assert d.hole(i)["titel"] == "Versicherungsschein"


def test_warteschlange_prioritaet_und_retry(tmp_path):
    d = _db(tmp_path)
    assert d.einreihen("/import/x.pdf", "import", "Ordner") is True
    assert d.einreihen("/import/x.pdf", "import", "Ordner") is False
    d.einreihen("/eingang/y.pdf", "eingang")
    n = d.naechster(jetzt=100.0)
    assert n["pfad"] == "/eingang/y.pdf"           # Eingang vor Import
    d.fehlschlag(n["id"], naechster_ts=200.0)
    assert d.naechster(jetzt=150.0)["pfad"] == "/import/x.pdf"
    assert d.hole_auftrag(n["id"])["versuche"] == 1
    d.entfernen(n["id"])
    assert d.zahlen()["warteschlange"] == 1


def test_sortierung(tmp_path):
    d = _db(tmp_path)
    titel = [t["titel"] for t in d.suche(sortierung="titel", absteigend=False)[0]]
    assert titel == ["Beitragsbescheid", "Police", "Steuerbescheid"]
    absender = [t["absender"] for t in d.suche(sortierung="absender", absteigend=True)[0]]
    assert absender == ["TK", "HUK", "Finanzamt Kelheim"]
    # unbekannte Sortierung fällt auf Datum zurück (kein SQL aus der Anfrage)
    assert d.suche(sortierung="1; DROP TABLE dokumente")[0][0]["datum"] == "2026-11-12"


def test_zaehlungen(tmp_path):
    d = _db(tmp_path)
    assert d.arten_zaehlung() == {"Versicherungen": 2, "Steuer": 1}
    z = {r["absender"]: r for r in d.absender_zaehlung()}
    assert z["TK"]["anzahl"] == 1 and z["TK"]["art"] == "Versicherungen" and z["TK"]["zuletzt"] == "2026-11-12"
