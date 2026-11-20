# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Zweiter Blick auf unsichere Dokumente + einheitlicher Titel für Lohnzettel."""
from datetime import date
from pathlib import Path

import pytest

import ki
import zweiter_blick as zb
from config import STANDARD_ARTEN
from datum import finde_zeitraum
from db import Db
from titel import gehaltsabrechnung, ist_gehaltsabrechnung
from verarbeitung import Worker

HEUTE = date(2026, 11, 13)
LOHN = ("Firma Muster GmbH Personalabteilung Abrechnung der Brutto Netto Bezüge für {monat} 2024 "
        "Personalnummer Steuerklasse Kirchensteuer Krankenversicherung Rentenversicherung Arbeitslosenversicherung "
        "Pflegeversicherung Lohnsteuer Solidaritätszuschlag Gesamtbrutto Nettobezug Auszahlungsbetrag "
        "Überweisung Bankverbindung Urlaubsanspruch Resturlaub Arbeitszeitkonto Grundgehalt Zulage {extra}")


def _nb(sim, art="Arbeit & Gehalt", absender="Firma Muster"):
    return zb.Nachbar(sim, 1, art, absender, "Gehaltsabrechnung")


# ── reine Regeln ─────────────────────────────────────────────────────────
def test_urteil_nachbarn_braucht_zwei_einige():
    assert zb.urteil_nachbarn([_nb(0.95)]) is None                          # ein Nachbar reicht nie
    u = zb.urteil_nachbarn([_nb(0.9), _nb(0.8), _nb(0.3, art="Steuer")])
    assert u.art == "Arbeit & Gehalt" and u.absender == "Firma Muster" and u.n == 2
    assert zb.urteil_nachbarn([_nb(0.9), _nb(0.8, art="Steuer")]) is None  # uneinig
    u = zb.urteil_nachbarn([_nb(0.9), _nb(0.8, absender="Andere AG")])
    assert u.art == "Arbeit & Gehalt" and u.absender is None                # Absender uneinig → behalten


def test_urteil_ki():
    assert zb.urteil_ki("Steuer", "Bank & Finanzen", ["anders als frühere Briefe dieses Absenders"], {"Steuer"}, True)
    assert not zb.urteil_ki("Steuer", "Bank & Finanzen", ["KI unsicher"], set(), True)  # widerspricht, keine Stütze
    assert zb.urteil_ki("Steuer", "Steuer", ["KI unsicher"], set(), True)               # zweimal gleich, nichts dagegen
    assert not zb.urteil_ki("Steuer", "Steuer", ["KI unsicher"], set(), False)          # aber nicht, wenn die KI zweifelt
    assert not zb.urteil_ki("Steuer", "Steuer", ["KI unsicher"], {"Wohnen"}, True)      # Stütze sagt etwas anderes
    assert not zb.urteil_ki("Sonstiges", "Sonstiges", ["keine passende Art"], set(), True)


def test_absender_wechsel_nur_mit_beleg():
    assert zb.absender_wechsel_ok("Unbekannt", "Firma Muster", "")
    assert zb.absender_wechsel_ok("Firma Muster GmbH", "Firma Muster", "Firma Muster GmbH, Postfach")
    assert not zb.absender_wechsel_ok("Beta Logistik AG", "Alpha Maschinenbau", "Beta Logistik AG Lohnabrechnung")
    assert zb.absender_wechsel_ok("FM", "Firma Muster", "", kanon=lambda s: "Firma Muster" if s == "FM" else s)


def test_zeitraum_und_gehaltstitel():
    assert finde_zeitraum("Abrechnungsmonat: März 2019", HEUTE) == date(2019, 3, 31)
    assert finde_zeitraum("Zeitraum 01.03.2019 - 31.03.2019", HEUTE) == date(2019, 3, 31)
    for kein_monat in ("Stand Mai 2018", "Formular Stand März 2019", "Beitrag pro Monat 9.99",
                       "Gebühr je Monat 5.90", "Zeitraum: 3.21", "Urlaubsjahr 01.01.2024 - 31.12.2024"):
        assert finde_zeitraum(kein_monat, HEUTE) is None, kein_monat
    assert finde_zeitraum("Urlaubsjahr 01.01.2024 - 31.12.2024 Bezüge für März 2024", HEUTE) == date(2024, 3, 31)
    assert finde_zeitraum("Monat " + " " * 6000 + "x", HEUTE) is None          # kein Hänger bei viel Leerraum
    d = date(2024, 4, 2)
    assert gehaltsabrechnung("Gehaltsabrechnung", "Bezüge für März 2024", d, HEUTE) == "Gehaltsabrechnung März 2024"
    assert gehaltsabrechnung("Lohnabrechnung 02/2024", "", d, HEUTE) == "Gehaltsabrechnung Februar 2024"
    assert gehaltsabrechnung("Entgeltabrechnung", "", d, HEUTE) == "Gehaltsabrechnung April 2024"
    assert gehaltsabrechnung("Gehaltsabrechnung März 2024", "Bezüge für März 2024", d, HEUTE) is None
    for nein in ("Lohnsteuerbescheinigung", "Arbeitsvertrag", "Jahresentgeltbescheinigung", "Rechnung",
                 "Lohnabrechnung Weihnachtsgeld", "Gehaltsabrechnung Mai 2024 Urlaubsgeld",
                 "Verdienstbescheinigung Elterngeld", "Gehaltsbescheinigung", "Lohnnachweis"):
        assert not ist_gehaltsabrechnung(nein)


def test_zweiter_blick_im_ki_text():
    text = ki._nutzertext("x" * 6000, "a.pdf", ["Steuer", "Sonstiges"], [], zweiter_blick={
        "gruende": ["KI unsicher"], "beispiele": [("Firma Muster", "Arbeit & Gehalt", "Gehaltsabrechnung")]})
    assert "ZWEITER, GENAUERER BLICK" in text and "„Firma Muster“, Art „Arbeit & Gehalt“" in text
    assert "x" * 5000 in text and "x" * 5001 not in text
    assert "ZWEITER" not in ki._nutzertext("x", "a.pdf", ["Steuer"], [])


# ── Arbeiter ─────────────────────────────────────────────────────────────
class KiFake:
    def __init__(self, art="Arbeit & Gehalt", absender="Firma Muster", fehler=None):
        self.art, self.absender, self.fehler, self.aufrufe = art, absender, fehler, []
        self.sicherheit, self.datum = 0.99, None

    def __call__(self, text, dateiname, arten, absender_bekannt, url, timeout=300, hinweise=None, zweiter_blick=None):
        self.aufrufe.append(zweiter_blick)
        if self.fehler:
            raise self.fehler
        return ki.Einordnung(self.art, self.absender, self.datum, "Gehaltsabrechnung", self.sicherheit)


@pytest.fixture
def u(tmp_path):
    d = {n: tmp_path / n for n in ("eingang", "archiv", "daten")}
    for p in d.values():
        p.mkdir()
    d["db"] = Db(d["daten"] / "t.sqlite")
    d["ki"] = KiFake()
    d["w"] = Worker(d["db"], lambda: {"arten": STANDARD_ARTEN, "unsicher_schwelle": 0.75}, ki_fn=d["ki"],
                    eingang=d["eingang"], archiv=d["archiv"], quellen=tmp_path, daten=d["daten"],
                    ki_url="http://ki", heute=lambda: HEUTE, ki_ok_fn=lambda: True)
    return d


def _dok(u, n, text, art="Arbeit & Gehalt", absender="Firma Muster", titel="Gehaltsabrechnung", grund=""):
    rel = f"{art}/{absender}/2024/2024-0{n}-28 {titel} {n}.pdf"
    (u["archiv"] / rel).parent.mkdir(parents=True, exist_ok=True)
    (u["archiv"] / rel).write_bytes(b"%PDF " + str(n).encode())
    return u["db"].einfuegen(text=text, sha256=f"s{n}", rel_pfad=rel, art=art, absender=absender,
                             datum=f"2024-0{n}-28", titel=titel, unsicher=1 if grund else 0, grund=grund,
                             erst_grund=grund)


def test_nachbarn_ordnen_unsicheren_lohnzettel_ein(u):
    _dok(u, 1, LOHN.format(monat="Januar", extra="Prämie"))
    _dok(u, 2, LOHN.format(monat="Februar", extra="Fahrtkosten"))
    i = _dok(u, 3, LOHN.format(monat="März", extra="Einmalzahlung"), art="Bank & Finanzen",
             absender="Firma Muster GmbH", titel="Lohnabrechnung", grund="anders als frühere Briefe dieses Absenders")
    assert u["w"].blick_nachbarn() == 1
    d = u["db"].hole(i)
    assert (d["art"], d["absender"], d["unsicher"], d["blick_geklaert"]) == ("Arbeit & Gehalt", "Firma Muster", 0, 1)
    assert d["titel"] == "Gehaltsabrechnung März 2024" and "2 fast gleiche" in d["blick_text"]
    assert "Bank & Finanzen" not in str(u["w"].status())       # Status-API nennt nur Zahlen
    assert (u["archiv"] / d["rel_pfad"]).is_file() and d["rel_pfad"].startswith("Arbeit & Gehalt/Firma Muster/")
    assert u["db"].blick_zahlen() == {"offen": 0, "geklaert": 1}
    # Speichert der Betreiber es selbst, ist es kein Fall mehr für die Stichproben-Liste.
    u["w"].korrigieren(i, d["art"], d["absender"], date(2024, 3, 28), d["titel"])
    assert u["db"].blick_zahlen()["geklaert"] == 0 and u["db"].hole(i)["blick_text"] == ""


def test_ohne_nachbarn_bleibt_unsicher_bis_neues_wissen(u):
    i = _dok(u, 3, LOHN.format(monat="März", extra="x"), art="Bank & Finanzen", grund="KI unsicher")
    assert u["w"].blick_nachbarn() == 0 and u["db"].hole(i)["unsicher"] == 1
    assert u["w"].blick_nachbarn() == 0                          # gleicher Wissensstand: nicht erneut
    _dok(u, 1, LOHN.format(monat="Januar", extra="y"))
    _dok(u, 2, LOHN.format(monat="Februar", extra="z"))
    u["db"].zaehle("wissen")                                     # z. B. nach einer Korrektur
    assert u["w"].blick_nachbarn() == 1 and u["db"].hole(i)["art"] == "Arbeit & Gehalt"


def test_ki_runde_klaert_mit_gleicher_antwort(u):
    i = _dok(u, 3, "Ein ganz eigener Brief ohne Vorbild " * 3, grund="KI unsicher")
    assert u["w"].blick_schritt() is True
    d = u["db"].hole(i)
    assert d["blick"] == 1 and d["unsicher"] == 0 and "zweite, gleiche Antwort" in d["blick_text"]
    assert u["ki"].aufrufe[0]["gruende"] == ["KI unsicher"]


def test_ki_runde_ohne_stuetze_aendert_nichts(u):
    u["ki"].art = "Steuer"
    i = _dok(u, 3, "Ein ganz eigener Brief ohne Vorbild " * 3, grund="KI unsicher")
    u["w"].blick_schritt()
    d = u["db"].hole(i)
    assert (d["art"], d["unsicher"], d["blick"]) == ("Arbeit & Gehalt", 1, 1)
    assert "meint diesmal „Steuer“" in d["blick_text"]
    assert u["w"].blick_schritt() is False                      # nur einmal je Dokument …
    assert u["w"].blick_wiederholen() == 1                      # … außer auf Wunsch
    assert u["w"].blick_schritt() is True


def test_ki_runde_nimmt_aehnliche_als_vorbild(u):
    _dok(u, 1, LOHN.format(monat="Januar", extra="a"))
    i = _dok(u, 3, LOHN.format(monat="März", extra="b") + " Sonderzahlung Nachberechnung Korrekturlauf "
             "Vormonate Differenzbetrag", art="Sonstiges", grund="keine passende Art")
    u["w"].blick_nachbarn()                                      # nur ein Nachbar → Stufe 1 entscheidet nicht
    assert u["db"].hole(i)["unsicher"] == 1
    u["w"].blick_schritt()
    assert u["ki"].aufrufe[-1]["beispiele"] == [("Firma Muster", "Arbeit & Gehalt", "Gehaltsabrechnung")]
    d = u["db"].hole(i)
    assert (d["art"], d["unsicher"]) == ("Arbeit & Gehalt", 0) and "Art geändert" in d["blick_text"]


def test_ki_nicht_erreichbar_versucht_spaeter_erneut(u):
    u["ki"].fehler = ki.KiNichtErreichbar("weg")
    i = _dok(u, 3, "Brief " * 20, grund="KI unsicher")
    u["w"].blick_schritt()
    assert u["db"].hole(i)["blick"] == 0 and u["w"].status()["ki_pause"]


def test_vorrang_fuer_neue_dokumente(u):
    _dok(u, 3, "Brief " * 20, grund="KI unsicher")
    u["db"].einreihen(str(u["eingang"] / "neu.pdf"), "eingang")
    assert u["w"].blick_schritt() is False and u["ki"].aufrufe == []


def test_bestand_lohnzettel_werden_einmal_umbenannt(u):
    i = _dok(u, 3, "Abrechnung der Brutto/Netto-Bezüge für März 2024", titel="Lohnabrechnung")
    j = _dok(u, 4, "Mietvertrag", art="Wohnen", absender="Vermieter", titel="Mietvertrag")
    assert u["w"].titel_vereinheitlichen() == 1
    d = u["db"].hole(i)
    assert d["titel"] == "Gehaltsabrechnung März 2024" and d["rel_pfad"].endswith("Gehaltsabrechnung März 2024.pdf")
    assert (u["archiv"] / d["rel_pfad"]).is_file() and u["db"].hole(j)["titel"] == "Mietvertrag"
    u["db"].aendern(i, titel="Lohnabrechnung")
    assert u["w"].titel_vereinheitlichen() == 0                 # nur einmal – danach gilt, was der Betreiber tippt


def test_unsichere_stuetzen_sich_nicht_gegenseitig(u):
    # Drei unsichere Briefe desselben Absenders, alle mit derselben (falschen) Art, KI zweifelt kaum.
    ids = [_dok(u, n, f"Brief Nummer {n} " * 6, art="Steuer", absender="Verein", grund="anders als frühere Briefe "
                "dieses Absenders") for n in (1, 2, 3)]
    u["ki"].art, u["ki"].absender = "Steuer", "Verein"
    u["w"].blick_schritt()
    assert u["db"].hole(ids[0])["unsicher"] == 1                # keine Stütze aus anderen unsicheren


def test_maschinell_geklaerte_sind_keine_nachbarn(u):
    _dok(u, 1, LOHN.format(monat="Januar", extra="a"))
    j = _dok(u, 2, LOHN.format(monat="Februar", extra="b"))
    u["db"].aendern(j, blick_geklaert=1)                         # selbst nur vom zweiten Blick geklärt
    i = _dok(u, 3, LOHN.format(monat="März", extra="c"), art="Bank & Finanzen", grund="KI unsicher")
    assert u["w"].blick_nachbarn() == 0 and u["db"].hole(i)["unsicher"] == 1


def test_fremder_absender_wird_nicht_uebernommen(u):
    _dok(u, 1, LOHN.format(monat="Januar", extra="a"))
    _dok(u, 2, LOHN.format(monat="Februar", extra="b"))
    text = LOHN.format(monat="März", extra="c").replace("Firma Muster GmbH", "Beta Logistik AG")
    i = _dok(u, 3, text, art="Bank & Finanzen", absender="Beta Logistik", grund="KI unsicher")
    u["w"].blick_nachbarn()
    d = u["db"].hole(i)
    assert (d["art"], d["absender"], d["unsicher"]) == ("Arbeit & Gehalt", "Beta Logistik", 0)


def test_gelernte_regel_schlaegt_nachbarn(u):
    _dok(u, 1, LOHN.format(monat="Januar", extra="a"))
    _dok(u, 2, LOHN.format(monat="Februar", extra="b"))
    u["db"].setze_regel_art("firma muster", "Steuer")
    i = _dok(u, 3, LOHN.format(monat="März", extra="c"), art="Steuer", grund="Regel weicht von KI ab")
    u["w"].blick_nachbarn()
    assert (u["db"].hole(i)["art"], u["db"].hole(i)["unsicher"]) == ("Steuer", 1)


def test_ki_zweifel_klaert_nicht(u):
    u["ki"].sicherheit = 0.05
    i = _dok(u, 3, "Ein ganz eigener Brief ohne Vorbild " * 3, grund="KI unsicher")
    u["w"].blick_schritt()
    assert u["db"].hole(i)["unsicher"] == 1


def test_ki_datum_nur_mit_beleg_im_text(u):
    u["ki"].datum = date(2099, 5, 5)
    i = _dok(u, 3, "Brief ohne erkennbares Datum " * 4, grund="kein Datum erkannt")
    u["w"].blick_schritt()
    d = u["db"].hole(i)
    assert d["unsicher"] == 1 and d["datum"] == "2024-03-28" and "Datum weiter unklar" in d["blick_text"]
    u["ki"].datum = date(2023, 6, 1)
    j = _dok(u, 4, "Schreiben vom Sommer 2023 ohne Tag " * 4, grund="kein Datum erkannt")
    u["w"].blick_schritt()
    assert (u["db"].hole(j)["datum"], u["db"].hole(j)["unsicher"]) == ("2023-06-01", 0)


def test_ki_faellt_dauernd_aus_dann_aufgeben(u):
    u["ki"].fehler = ki.KiNichtErreichbar("weg")
    i = _dok(u, 3, "Brief " * 20, grund="KI unsicher")
    for _ in range(4):
        u["w"]._pause_bis = 0
        u["w"].blick_schritt()
    d = u["db"].hole(i)
    assert d["blick"] == 1 and "nicht geantwortet" in d["blick_text"] and d["unsicher"] == 1


def test_handkorrektur_waehrend_ki_runde_gewinnt(u):
    i = _dok(u, 3, "Ein ganz eigener Brief ohne Vorbild " * 3, grund="KI unsicher")
    original = u["ki"].__call__

    def mit_korrektur(*a, **k):
        u["w"].korrigieren(i, "Wohnen", "Vermieter", date(2024, 3, 1), "Mietvertrag")
        return original(*a, **k)
    u["w"].ki_fn = mit_korrektur
    u["w"].blick_schritt()
    d = u["db"].hole(i)
    assert (d["art"], d["titel"], d["blick_geklaert"]) == ("Wohnen", "Mietvertrag", 0)
