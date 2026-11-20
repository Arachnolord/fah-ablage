# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Einordnung durch das lokale Sprachmodell (llama.cpp-Server im eigenen Container).

Antwort als JSON nach festem Schema; die Art ist auf die eingestellte Liste
beschränkt. Die Sicherheit kommt aus den Token-Wahrscheinlichkeiten des
Art-Werts (Produkt), nicht aus einer Selbstauskunft des Modells.
"""
import json
import math
import re
from dataclasses import dataclass
from datetime import date

import httpx

MAX_ZEICHEN = 2500
MAX_ZEICHEN_ZWEITER_BLICK = 5000
MAX_ABSENDER_LISTE = 150

HINWEISE = {
    "Versicherungen": "alle Versicherungen, auch Krankenkassen (TK, AOK, Barmer, DAK …), Kfz, Haftpflicht, Hausrat, Leben",
    "Rente": "Rentenversicherung (DRV), Renteninformation, Rentenbescheid, Betriebsrente, Riester",
    "Steuer": "Finanzamt, Steuerbescheid, Steuererklärung, Lohnsteuerbescheinigung, Grundsteuer",
    "Bank & Finanzen": "Bank, Konto, Kontoauszug, Kredit, Depot, Bausparen",
    "Gesundheit": "Arzt, Zahnarzt, Klinik, Apotheke, Befund, Arzt- und Klinikrechnungen",
    "Wohnen": "Miete, Vermieter, Hausverwaltung, Wohnungskauf, Grundbuch, Handwerker am Haus",
    "Energie & Nebenkosten": "Strom, Gas, Wasser, Heizung, Müll, Nebenkostenabrechnung",
    "Telefon & Internet": "Mobilfunk, Festnetz, DSL, Glasfaser, Rundfunkbeitrag",
    "Arbeit & Gehalt": "Arbeitgeber, Gehaltsabrechnung, Arbeitsvertrag, Zeugnis, Bewerbung",
    "Fahrzeuge": "Auto, Motorrad, Zulassung, Werkstatt, TÜV, Leasing, Fahrzeugkauf",
    "Behörden & Ausweise": "Gemeinde, Landratsamt, Ausweis, Reisepass, Führerschein, Meldebescheinigung",
    "Familie & Schule": "Kinder, Kita, Schule, Kindergeld, Elterngeld, Familienkasse",
    "Verträge & Abos": "sonstige Verträge, Mitgliedschaften, Vereine, Abos, Streaming",
    "Rechnungen & Belege": "Kaufbelege und Rechnungen von Händlern, Online-Bestellungen, Garantie",
    "Sonstiges": "nur wenn nichts anderes passt",
}

SYSTEM = ("Du ordnest Briefe und Dokumente für ein privates Archiv in Deutschland ein. "
          "Du antwortest ausschließlich mit dem verlangten JSON.")


class KiFehler(RuntimeError):
    """KI-Antwort unbrauchbar (Meldung ohne Inhalt)."""


class KiNichtErreichbar(KiFehler):
    """KI antwortet nicht (Verbindung, Zeitlimit, 5xx) – kein Fehler des Dokuments."""


def erreichbar(url: str, timeout: float = 5, transport=None) -> bool:
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            return c.get(url.rstrip("/") + "/health").status_code == 200
    except httpx.HTTPError:
        return False


@dataclass
class Einordnung:
    art: str
    absender: str
    datum: date | None
    titel: str
    sicherheit: float


def _schema(arten: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "absender": {"type": "string", "maxLength": 40},
            "titel": {"type": "string", "maxLength": 60},
            "datum": {"type": "string", "maxLength": 10},
            "art": {"type": "string", "enum": list(arten)},
        },
        "required": ["absender", "titel", "datum", "art"],
        "additionalProperties": False,
    }


def _nutzertext(text: str, dateiname: str, arten: list[str], absender_bekannt: list[str],
                hinweise: dict | None = None, zweiter_blick: dict | None = None) -> str:
    alle_hinweise = {**HINWEISE, **{k: v for k, v in (hinweise or {}).items() if v}}
    zeilen = ["Ordne das folgende Dokument ein.", "", "Arten (genau eine wählen):"]
    for a in arten:
        h = alle_hinweise.get(a)
        zeilen.append(f"- {a}: {h}" if h else f"- {a}")
    zeilen += [
        "",
        "absender: die Firma, Behörde oder Person, die das Dokument geschickt oder ausgestellt hat – "
        "NICHT der Empfänger. Kurz und gängig, z. B. „TK“ statt „Techniker Krankenkasse“, "
        "„DRV“ statt „Deutsche Rentenversicherung Bund“, „Finanzamt Kelheim“.",
    ]
    zeilen += [
        "titel: was für ein Dokument es ist, 1–4 Wörter, ohne Absender und ohne Datum "
        "(z. B. „Beitragsbescheid“, „Rechnung“, „Renteninformation“, „Kündigungsbestätigung“; "
        "Lohn- und Gehaltszettel immer „Gehaltsabrechnung“).",
        "datum: Briefdatum bzw. Ausstellungsdatum als JJJJ-MM-TT; leer lassen, wenn keins erkennbar. "
        "Nicht das Geburtsdatum, nicht Fristen oder Zeiträume.",
    ]
    # Gleichbleibender Teil zuerst: llama.cpp nutzt den Zwischenspeicher für den gemeinsamen Anfang.
    if absender_bekannt:
        liste = ", ".join(absender_bekannt[:MAX_ABSENDER_LISTE])
        zeilen.append(f"Schon bekannte Absender (genau so schreiben, wenn es derselbe ist): {liste}")
    max_zeichen = MAX_ZEICHEN
    if zweiter_blick is not None:
        max_zeichen = MAX_ZEICHEN_ZWEITER_BLICK
        zeilen += ["", "ZWEITER, GENAUERER BLICK: Beim ersten Durchgang war die Einordnung unsicher"
                   + (f" ({', '.join(zweiter_blick.get('gruende') or [])})." if zweiter_blick.get("gruende") else ".")
                   + " Lies den Text gründlich, auch weiter unten.",
                   "Hat das Dokument kein Briefdatum, aber einen Abrechnungsmonat oder -zeitraum, "
                   "nimm als datum dessen letzten Tag."]
        beispiele = zweiter_blick.get("beispiele") or []
        if beispiele:
            zeilen.append("So wurden ähnliche, schon geprüfte Dokumente eingeordnet – übernimm Art und "
                          "Schreibweise des Absenders, wenn es dieselbe Sorte Dokument ist:")
            zeilen += [f"- Absender „{ab}“, Art „{art}“, Titel „{ti}“" for ab, art, ti in beispiele]
    zeilen += [
        "",
        f"Dateiname: {dateiname}",
        "Text (Anfang des Dokuments):",
        "<<<",
        (text or "")[:max_zeichen],
        ">>>",
    ]
    return "\n".join(zeilen)


def _sicherheit(inhalt: str, art: str, logprobs) -> float:
    try:
        tokens = logprobs["content"]
    except (TypeError, KeyError):
        return 0.5
    if not tokens:
        return 0.5
    teile = [t.get("token", "") for t in tokens]
    if "".join(teile) != inhalt:
        return 0.5
    m = re.search(r'"art"\s*:\s*"', inhalt)
    if not m:
        return 0.5
    anfang, ende = m.end(), m.end() + len(art)
    pos, summe = 0, 0.0
    for teil, t in zip(teile, tokens):
        t_anfang, t_ende = pos, pos + len(teil)
        pos = t_ende
        if t_ende <= anfang or t_anfang >= ende:
            continue
        summe += float(t.get("logprob", 0.0))
    return max(0.0, min(1.0, math.exp(summe)))


def _datum(s: str) -> date | None:
    try:
        d = date.fromisoformat((s or "").strip())
    except ValueError:
        return None
    return d if d.year >= 1950 else None


def ordne_ein(text: str, dateiname: str, arten: list[str], absender_bekannt: list[str],
              url: str, timeout: float = 300, transport=None, hinweise: dict | None = None,
              zweiter_blick: dict | None = None) -> Einordnung:
    """zweiter_blick: {"gruende": [...], "beispiele": [(absender, art, titel), ...]} für die genauere zweite Runde."""
    body = {
        "model": "lokal",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _nutzertext(text, dateiname, arten, absender_bekannt, hinweise, zweiter_blick)},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "logprobs": True,
        "top_logprobs": 3,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "einordnung", "strict": True, "schema": _schema(arten)}},
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(url.rstrip("/") + "/v1/chat/completions", json=body)
    except httpx.HTTPError as e:
        raise KiNichtErreichbar(f"KI nicht erreichbar ({type(e).__name__})") from None
    if r.status_code >= 500:
        raise KiNichtErreichbar(f"KI antwortet mit HTTP {r.status_code}")
    if r.status_code != 200:
        raise KiFehler(f"KI antwortet mit HTTP {r.status_code}")
    try:
        choice = r.json()["choices"][0]
        inhalt = choice["message"]["content"]
        d = json.loads(inhalt)
        art = d["art"]
        absender = " ".join(str(d["absender"]).split())[:40]
        titel = " ".join(str(d["titel"]).split())[:60]
    except (ValueError, KeyError, IndexError, TypeError):
        raise KiFehler("KI-Antwort unbrauchbar") from None
    if art not in arten:
        raise KiFehler("KI-Antwort: Art nicht in der Liste")
    return Einordnung(art=art, absender=absender, datum=_datum(d.get("datum", "")),
                      titel=titel, sicherheit=_sicherheit(inhalt, art, choice.get("logprobs")))
