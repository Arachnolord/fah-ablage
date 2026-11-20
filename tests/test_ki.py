# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
import json
import math
from datetime import date

import httpx
import pytest

import ki
from config import STANDARD_ARTEN


def _antwort(inhalt: str, tokens=None):
    choice = {"message": {"role": "assistant", "content": inhalt}}
    if tokens is not None:
        choice["logprobs"] = {"content": [{"token": t, "logprob": lp} for t, lp in tokens]}
    return {"choices": [choice]}


def _transport(antwort=None, status=200, fehler=None, merke=None):
    def handler(req):
        if merke is not None:
            merke.append(json.loads(req.content))
        if fehler:
            raise fehler
        return httpx.Response(status, json=antwort)
    return httpx.MockTransport(handler)


INHALT = '{"absender": "TK", "titel": "Beitragsbescheid", "datum": "2026-11-12", "art": "Versicherungen"}'


def _tokens_fuer(inhalt, art_probs):
    """Zerlegt den Inhalt so, dass der Art-Wert aus zwei Tokens mit den angegebenen Wahrscheinlichkeiten besteht."""
    start = inhalt.index('"Versicherungen"') + 1
    vorne, art, hinten = inhalt[:start], inhalt[start:start + 14], inhalt[start + 14:]
    return [(vorne, 0.0), (art[:4], math.log(art_probs[0])), (art[4:], math.log(art_probs[1])), (hinten, 0.0)]


def test_gueltige_antwort_mit_sicherheit():
    t = _transport(_antwort(INHALT, _tokens_fuer(INHALT, (0.9, 0.5))))
    e = ki.ordne_ein("Text", "scan.pdf", STANDARD_ARTEN, ["TK"], "http://ki", transport=t)
    assert (e.art, e.absender, e.titel, e.datum) == ("Versicherungen", "TK", "Beitragsbescheid", date(2026, 11, 12))
    assert abs(e.sicherheit - 0.45) < 1e-6


def test_ohne_logprobs_halbe_sicherheit_und_leeres_datum():
    inhalt = INHALT.replace("2026-11-12", "")
    e = ki.ordne_ein("Text", "x.pdf", STANDARD_ARTEN, [], "http://ki", transport=_transport(_antwort(inhalt)))
    assert e.sicherheit == 0.5 and e.datum is None


def test_art_ausserhalb_der_liste():
    t = _transport(_antwort(INHALT.replace("Versicherungen", "Krankenkasse")))
    with pytest.raises(ki.KiFehler):
        ki.ordne_ein("Text", "x.pdf", STANDARD_ARTEN, [], "http://ki", transport=t)


def test_kaputtes_json():
    with pytest.raises(ki.KiFehler):
        ki.ordne_ein("Text", "x.pdf", STANDARD_ARTEN, [], "http://ki", transport=_transport(_antwort("{kaputt")))


def test_zeitueberschreitung_und_serverfehler():
    with pytest.raises(ki.KiFehler):
        ki.ordne_ein("T", "x.pdf", STANDARD_ARTEN, [], "http://ki",
                     transport=_transport(fehler=httpx.ReadTimeout("zu langsam")))
    with pytest.raises(ki.KiFehler):
        ki.ordne_ein("T", "x.pdf", STANDARD_ARTEN, [], "http://ki", transport=_transport({}, status=503))


def test_anfrage_kuerzt_text_und_nutzt_schema():
    merke = []
    ki.ordne_ein("x" * 10_000, "x.pdf", STANDARD_ARTEN, ["TK", "DRV"], "http://ki",
                 transport=_transport(_antwort(INHALT), merke=merke))
    body = merke[0]
    nutzer = body["messages"][-1]["content"]
    assert nutzer.count("x") <= ki.MAX_ZEICHEN + 50
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["art"]["enum"] == STANDARD_ARTEN
    assert body["temperature"] == 0 and body["logprobs"] is True
    assert "TK" in nutzer and "DRV" in nutzer
    assert "Krankenkasse" in nutzer   # Hinweis zur Art Versicherungen


def test_nicht_erreichbar_ist_eigene_art():
    with pytest.raises(ki.KiNichtErreichbar):
        ki.ordne_ein("T", "x.pdf", STANDARD_ARTEN, [], "http://ki",
                     transport=_transport(fehler=httpx.ConnectError("weg")))
    with pytest.raises(ki.KiNichtErreichbar):
        ki.ordne_ein("T", "x.pdf", STANDARD_ARTEN, [], "http://ki", transport=_transport({}, status=503))
    try:
        ki.ordne_ein("T", "x.pdf", STANDARD_ARTEN, [], "http://ki", transport=_transport(_antwort("{kaputt")))
    except ki.KiNichtErreichbar:
        raise AssertionError("unbrauchbare Antwort ist kein Ausfall")
    except ki.KiFehler:
        pass


def test_erreichbar():
    assert ki.erreichbar("http://ki", transport=_transport({"status": "ok"})) is True
    assert ki.erreichbar("http://ki", transport=_transport({}, status=503)) is False
    assert ki.erreichbar("http://ki", transport=_transport(fehler=httpx.ConnectError("weg"))) is False


def test_eigene_hinweise_im_prompt():
    merke = []
    ki.ordne_ein("Text", "x.pdf", ["Haustiere", "Versicherungen", "Sonstiges"], [], "http://ki",
                 transport=_transport(_antwort(INHALT), merke=merke),
                 hinweise={"Haustiere": "Tierarzt, Hundesteuer", "Versicherungen": "nur Kfz"})
    nutzer = merke[0]["messages"][-1]["content"]
    assert "- Haustiere: Tierarzt, Hundesteuer" in nutzer
    assert "- Versicherungen: nur Kfz" in nutzer          # eigener Hinweis schlägt den eingebauten
