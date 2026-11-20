# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Weboberfläche: Übersicht + Upload, Suche, Dokument korrigieren, Einstellungen, Bericht."""
import ipaddress
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import threading

import ablage
import auth
import config
import extrakt
import ki
import ordnerwahl
import sicherung
import vorschau
from db import Db
from verarbeitung import Worker
from version import VERSION

APP_DIR = Path(__file__).resolve().parent
COOKIE = "ablage_sitzung"
MAX_UPLOAD = 200 * 1024 * 1024
BREMSE_MAX, BREMSE_FENSTER = 5, 600
SAMMEL_MAX = 200   # so viele Dokumente auf einmal (eine Seite hat 50)
_fehlversuche: dict[str, list[float]] = {}
_OEFFENTLICH = ("/anmelden", "/einrichten", "/static/")
CSP = ("default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'self'; "
       "base-uri 'none'; form-action 'self'")
# Nur diese Typen zeigt der Browser direkt an – alles andere (HTML, SVG …) nur als Download.
INLINE_TYPEN = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8"}
DATEI_WEG = "Die Datei liegt nicht mehr am erwarteten Platz – vielleicht im Finder verschoben oder umbenannt."
log = logging.getLogger("ablage")

SCHWELLEN_TEXT = [
    (0.0, "Nie markieren – alles gilt als sicher."),
    (0.5, "Locker – nur deutlich unklare Fälle werden markiert."),
    (0.7, "Ausgewogen – empfohlen."),
    (0.85, "Streng – auch knappe Entscheidungen werden markiert."),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def _bremse_aktiv(ip: str) -> bool:
    jetzt = time.time()
    liste = [t for t in _fehlversuche.get(ip, []) if jetzt - t < BREMSE_FENSTER]
    _fehlversuche[ip] = liste
    return len(liste) >= BREMSE_MAX


def _host_erlaubt(host_header: str) -> bool:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        name = h[1:].split("]")[0]
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if not name:
        return False
    if name == "localhost" or name in config.ERLAUBTE_HOSTS:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


class _OhneInhalt(logging.Filter):
    """Fehlermeldungen von uvicorn ohne Traceback: der enthielte Pfade mit Absender und Titel."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            typ = record.exc_info[0].__name__ if record.exc_info[0] else "Fehler"
            record.msg, record.args = "Unerwarteter Fehler (%s)", (typ,)
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


logging.getLogger("uvicorn.error").addFilter(_OhneInhalt())


def _gleiche_herkunft(request: Request) -> bool:
    host = request.headers.get("host", "")
    quelle = request.headers.get("origin") or request.headers.get("referer") or ""
    return bool(host) and urlparse(quelle).netloc == host


def _datum(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def erstelle_app(db: Db | None = None, worker: Worker | None = None, start_worker: bool = True,
                 archiv: Path | None = None, eingang: Path | None = None) -> FastAPI:
    archiv = Path(archiv or config.ARCHIV_DIR)
    eingang = Path(eingang or config.EINGANG_DIR)
    db = db or Db(config.DB_DATEI)
    worker = worker or Worker(db, config.lade_einstellungen)
    db.ordner_nachtragen(config.STANDARD_IMPORT_ORDNER, str(config.QUELLEN_DIR))

    @asynccontextmanager
    async def lebenszeit(app):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(lifespan=lebenszeit, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    tpl = Jinja2Templates(directory=APP_DIR / "templates")
    tpl.env.globals.update(version=VERSION)
    tpl.env.filters["zeit_de"] = lambda ts: time.strftime("%d.%m.%Y", time.localtime(ts)) if ts else ""

    def seite(request: Request, name: str, status: int = 200, **ctx):
        ctx.setdefault("angemeldet", bool(auth.read_session(request.cookies.get(COOKIE, ""))))
        return tpl.TemplateResponse(request, name, ctx, status_code=status)

    @app.middleware("http")
    async def waechter(request: Request, call_next):
        pfad = request.url.path
        if not _host_erlaubt(request.headers.get("host", "")):
            return Response("Unbekannter Hostname.", status_code=400)
        if not auth.zugang_lesbar():
            return Response("Die Zugangsdatei ist nicht lesbar. Bitte config/users.json prüfen.", status_code=503)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _gleiche_herkunft(request):
            return Response("Anfrage von fremder Seite abgelehnt.", status_code=403)
        if not pfad.startswith(_OEFFENTLICH):
            if not auth.has_admin():
                return RedirectResponse("/einrichten", status_code=303)
            if not auth.read_session(request.cookies.get(COOKIE, "")):
                if pfad.startswith(("/api/", "/datei/", "/vorschau/")):
                    return JSONResponse({"fehler": "nicht angemeldet"}, status_code=401)
                return RedirectResponse("/anmelden", status_code=303)
        antwort = await call_next(request)
        antwort.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        antwort.headers.setdefault("X-Content-Type-Options", "nosniff")
        antwort.headers.setdefault("Referrer-Policy", "same-origin")
        antwort.headers.setdefault("Cache-Control", "no-store")
        if not pfad.startswith("/datei/"):
            antwort.headers.setdefault("Content-Security-Policy", CSP)
        return antwort

    # ── Anmeldung ────────────────────────────────────────────────────────
    @app.get("/einrichten")
    def einrichten_seite(request: Request):
        if auth.has_admin():
            return RedirectResponse("/anmelden", status_code=303)
        return seite(request, "einrichten.html")

    @app.post("/einrichten")
    def einrichten(request: Request, benutzer: str = Form(""), passwort: str = Form("")):
        try:
            auth.setup_admin(benutzer, passwort)
        except auth.SchonEingerichtet:
            return Response("Bereits eingerichtet.", status_code=403)
        except ValueError as e:
            return seite(request, "einrichten.html", 400, fehler=str(e), benutzer=benutzer)
        return RedirectResponse("/anmelden", status_code=303)

    @app.get("/anmelden")
    def anmelden_seite(request: Request):
        return seite(request, "anmelden.html")

    @app.post("/anmelden")
    def anmelden(request: Request, benutzer: str = Form(""), passwort: str = Form("")):
        ip = request.client.host if request.client else "?"
        if _bremse_aktiv(ip):
            return seite(request, "anmelden.html", 429, benutzer=benutzer,
                         fehler="Zu viele Fehlversuche. Bitte 10 Minuten warten.")
        if not auth.verify(benutzer, passwort):
            _fehlversuche.setdefault(ip, []).append(time.time())
            return seite(request, "anmelden.html", 401, benutzer=benutzer,
                         fehler="Benutzername oder Passwort stimmt nicht.")
        _fehlversuche.pop(ip, None)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, auth.make_session(benutzer.strip()), httponly=True, samesite="strict",
                     max_age=auth.SESSION_MAX_AGE)
        return r

    @app.post("/abmelden")
    def abmelden():
        r = RedirectResponse("/anmelden", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    # ── Übersicht + Upload ───────────────────────────────────────────────
    @app.get("/")
    def uebersicht(request: Request):
        zuletzt, _ = db.suche(limit=12, sortierung="eingang")
        return seite(request, "uebersicht.html", zahlen=db.zahlen(),
                     lauf=worker.status(), zuletzt=zuletzt, arten=config.lade_einstellungen()["arten"],
                     pfade=config.ANZEIGE_PFADE)

    @app.get("/api/status")
    def api_status():
        return {**worker.status(), **db.zahlen(), "sicherung": dict(sich)}

    @app.post("/api/upload")
    async def upload(dateien: list[UploadFile]):
        eingang.mkdir(parents=True, exist_ok=True)
        angenommen, abgelehnt = 0, 0
        for f in dateien:
            roh = Path((f.filename or "upload").replace("\\", "/")).name
            endung = re.sub(r"[^A-Za-z0-9]", "", Path(roh).suffix)[:8].lower()
            stamm = ablage.saeubere(Path(roh).stem if endung else roh, 90)
            name = f"{stamm}.{endung}" if endung else stamm
            ziel = eingang / name
            n = 2
            while ziel.exists() or ziel.with_name(ziel.name + ".part").exists():
                ziel = eingang / f"{Path(name).stem} ({n}){Path(name).suffix}"
                n += 1
            teil = ziel.with_name(ziel.name + ".part")
            groesse = 0
            try:
                with open(teil, "wb") as out:
                    while block := await f.read(1 << 20):
                        groesse += len(block)
                        if groesse > MAX_UPLOAD:
                            raise ValueError("zu groß")
                        out.write(block)
                if groesse == 0:
                    raise ValueError("leer")
                os.replace(teil, ziel)
            except (ValueError, OSError):
                teil.unlink(missing_ok=True)
                abgelehnt += 1
                continue
            db.einreihen(str(ziel), "upload")
            angenommen += 1
        worker.wecken()
        return {"angenommen": angenommen, "abgelehnt": abgelehnt}

    # ── Dokumente (Liste wie in Paperless: Tabelle oder Kacheln) ─────────
    SORTIERUNGEN = {"datum": "Datum", "titel": "Titel", "absender": "Absender", "art": "Art", "eingang": "Hinzugefügt"}

    @app.get("/dokumente")
    @app.get("/suche")
    def dokumente(request: Request, q: str = "", art: str = "", absender: str = "", von: str = "",
                  bis: str = "", jahr: str = "", unsicher: str = "", papierkorb: str = "", seite_nr: int = 1,
                  blick: str = "",
                  ansicht: str = "", sortierung: str = "datum", richtung: str = "ab"):
        if jahr.isdigit() and len(jahr) == 4:
            von, bis = f"{jahr}-01-01", f"{jahr}-12-31"
        ansicht = ansicht if ansicht in ("tabelle", "kacheln") else request.cookies.get("ablage_ansicht", "tabelle")
        if ansicht not in ("tabelle", "kacheln"):
            ansicht = "tabelle"
        sortierung = sortierung if sortierung in SORTIERUNGEN else "datum"
        absteigend = richtung != "auf"
        groesse = 50
        seite_nr = max(1, seite_nr)
        treffer, gesamt = db.suche(art=art or None, absender=absender or None, von=von or None,
                                   bis=bis or None, text=q or None, nur_unsicher=bool(unsicher), nur_blick=bool(blick),
                                   limit=groesse, offset=(seite_nr - 1) * groesse,
                                   status="papierkorb" if papierkorb else "abgelegt",
                                   sortierung=sortierung, absteigend=absteigend)
        jahre = list(range(date.today().year, date.today().year - 8, -1))
        filter_q = {k: v for k, v in dict(q=q, art=art, absender=absender, von=von if not jahr else "",
                                          bis=bis if not jahr else "", jahr=jahr, unsicher=unsicher,
                                          papierkorb=papierkorb, blick=blick).items() if v}
        def link(**aenderung) -> str:
            werte = {**filter_q, "ansicht": ansicht, "sortierung": sortierung,
                     "richtung": "ab" if absteigend else "auf", **aenderung}
            if werte.get("seite_nr") in (1, "1"):
                werte.pop("seite_nr")
            return "/dokumente?" + urlencode({k: v for k, v in werte.items() if v not in ("", None)})

        def sort_link(schluessel: str) -> str:
            umdrehen = sortierung == schluessel and absteigend
            return link(sortierung=schluessel, richtung="auf" if umdrehen else "ab", seite_nr=1)

        antwort = seite(request, "dokumente.html", link=link, sort_link=sort_link, treffer=treffer, gesamt=gesamt, q=q, art=art,
                        absender=absender, von=von, bis=bis, jahr=jahr, unsicher=unsicher, blick=blick,
                        papierkorb=papierkorb, seite_nr=seite_nr, seiten=max(1, -(-gesamt // groesse)),
                        groesse=groesse, arten=config.lade_einstellungen()["arten"],
                        absender_liste=db.absender_liste(art or None), jahre=jahre, ansicht=ansicht,
                        sortierung=sortierung, absteigend=absteigend, sortierungen=SORTIERUNGEN,
                        filter_q=filter_q, absender_alle=db.absender_liste())
        antwort.set_cookie("ablage_ansicht", ansicht, max_age=365 * 86400, samesite="strict", httponly=True)
        return antwort

    @app.post("/sammel")
    def sammel(request: Request, ids: list[int] = Form([]), aktion: str = Form(""), art: str = Form(""),
               absender: str = Form(""), datum: str = Form(""), zurueck: str = Form("/dokumente")):
        """Mehrfachauswahl: Art/Absender/Datum für alle setzen (= geprüft), Papierkorb, Wiederherstellen."""
        ziel = urlparse(zurueck)
        if ziel.scheme or ziel.netloc or ziel.path not in ("/dokumente", "/suche"):
            ziel = urlparse("/dokumente")
        rest = [(k, v) for k, v in parse_qsl(ziel.query)
                if k not in ("erledigt", "was", "fehlgeschlagen", "fertig", "seite_nr")]   # Liste wird kürzer
        ids = list(dict.fromkeys(ids))[:SAMMEL_MAX]
        absender = " ".join(absender.split())[:50]
        dt = _datum(datum) if datum.strip() else None
        if aktion not in ("speichern", "papierkorb", "wiederherstellen", "endgueltig") or not ids:
            return RedirectResponse(ziel.path + ("?" + urlencode(rest) if rest else ""), status_code=303)
        if aktion == "speichern" and ((art and art not in config.lade_einstellungen()["arten"])
                                      or (datum.strip() and not dt)):
            return seite(request, "fehler.html", 400, meldung="Diese Art oder dieses Datum gibt es nicht.")
        erwartet = "papierkorb" if aktion in ("wiederherstellen", "endgueltig") else "abgelegt"
        ok = fehler = 0
        for dok_id in ids:
            d = db.hole(dok_id)
            if not d or d["status"] != erwartet:
                fehler += 1
                continue
            try:
                if aktion == "speichern":
                    worker.korrigieren(dok_id, art or d["art"], absender or d["absender"],
                                       dt or date.fromisoformat(d["datum"]), d["titel"], lernen=False)
                elif aktion == "papierkorb":
                    worker.in_papierkorb(dok_id)
                elif aktion == "endgueltig":
                    if not worker.endgueltig_loeschen(dok_id):
                        raise OSError("nicht gelöscht")
                else:
                    worker.wiederherstellen(dok_id)
                ok += 1
            except (OSError, KeyError, ValueError) as e:
                fehler += 1
                log.warning("dok=%d sammel=%s fehler=%s", dok_id, aktion, type(e).__name__)
        log.info("sammel=%s ok=%d fehler=%d", aktion, ok, fehler)
        rest += [("erledigt", ok), ("was", aktion), ("fehlgeschlagen", fehler)]
        return RedirectResponse(ziel.path + "?" + urlencode(rest), status_code=303)

    @app.post("/papierkorb/leeren")
    def papierkorb_leeren():
        n = worker.papierkorb_leeren()
        return RedirectResponse(f"/dokumente?papierkorb=1&erledigt={n}&was=endgueltig&fehlgeschlagen=0", status_code=303)

    @app.post("/dokument/{dok_id}/endgueltig")
    def dokument_endgueltig(dok_id: int):
        ok = worker.endgueltig_loeschen(dok_id)
        return RedirectResponse(f"/dokumente?papierkorb=1&erledigt={int(ok)}&was=endgueltig&fehlgeschlagen={int(not ok)}",
                                status_code=303)

    @app.get("/absender")
    def absender_seite(request: Request):
        return seite(request, "absender.html", liste=db.absender_zaehlung())

    @app.get("/arten")
    def arten_seite(request: Request, fehler: str = ""):
        e = config.lade_einstellungen()
        zaehlung = db.arten_zaehlung()
        liste = [(a, zaehlung.get(a, 0)) for a in e["arten"]]
        fremde = [(a, n) for a, n in zaehlung.items() if a not in e["arten"]]
        return seite(request, "arten.html", liste=liste, fremde=fremde, eigene=e["art_hinweise"],
                     eingebaut=ki.HINWEISE)

    def arten_fehler(request: Request, meldung: str):
        return seite(request, "fehler.html", 409, meldung=meldung, zurueck="/arten")

    @app.post("/arten/neu")
    def art_neu(request: Request, name: str = Form(""), hinweis: str = Form("")):
        e = config.lade_einstellungen()
        n = ablage.saeubere(name, 40)
        if not name.strip() or any(a.casefold() == n.casefold() for a in e["arten"]):
            return arten_fehler(request, "Diese Art gibt es schon (oder der Name ist leer).")
        stelle = len(e["arten"]) - 1 if e["arten"] and e["arten"][-1] == "Sonstiges" else len(e["arten"])
        e["arten"].insert(stelle, n)
        if hinweis.strip():
            e["art_hinweise"][n] = " ".join(hinweis.split())[:200]
        config.speichere_einstellungen(e)
        return RedirectResponse("/arten", status_code=303)

    @app.post("/arten/umbenennen")
    def art_umbenennen(request: Request, alt: str = Form(""), neu: str = Form(""), hinweis: str = Form("")):
        e = config.lade_einstellungen()
        if alt not in e["arten"]:
            return arten_fehler(request, "Diese Art gibt es nicht.")
        n = ablage.saeubere(neu or alt, 40)
        if n != alt:
            if alt == "Sonstiges":
                return arten_fehler(request, "„Sonstiges“ lässt sich nicht umbenennen.")
            if any(a.casefold() == n.casefold() for a in e["arten"] if a != alt):
                return arten_fehler(request, "Eine Art mit diesem Namen gibt es schon.")
            try:
                n = worker.art_umbenennen(alt, n)
            except (ValueError, OSError) as fehler:
                log.warning("art umbenennen fehler=%s", type(fehler).__name__)
                return arten_fehler(request, str(fehler) if isinstance(fehler, ValueError) else DATEI_WEG)
            e["arten"][e["arten"].index(alt)] = n
            e["art_hinweise"].pop(alt, None)
        if hinweis.strip():
            e["art_hinweise"][n] = " ".join(hinweis.split())[:200]
        else:
            e["art_hinweise"].pop(n, None)
        config.speichere_einstellungen(e)
        return RedirectResponse("/arten", status_code=303)

    @app.post("/arten/loeschen")
    def art_loeschen(request: Request, name: str = Form("")):
        e = config.lade_einstellungen()
        if name == "Sonstiges" or name not in e["arten"]:
            return arten_fehler(request, "„Sonstiges“ bleibt immer erhalten.")
        n = db.anzahl_art(name)
        if n:
            return arten_fehler(request, f"In „{name}“ liegen noch {n} Dokumente (auch im Papierkorb). "
                                         "Erst umsortieren oder die Art umbenennen.")
        e["arten"].remove(name)
        e["art_hinweise"].pop(name, None)
        config.speichere_einstellungen(e)
        try:
            (archiv / ablage.saeubere(name, 40)).rmdir()
        except OSError:
            pass
        return RedirectResponse("/arten", status_code=303)

    def datei_von(d: dict) -> Path:
        return Path(d["papierkorb_pfad"]) if d["status"] == "papierkorb" else archiv / d["rel_pfad"]

    def text_von(d: dict) -> str:
        """Erkannter Text; fehlt er bei schnell lesbaren Typen (z. B. .xls vor 1.4.2), wird er nachgeholt."""
        t = db.text(d["id"])
        p = datei_von(d)
        if t.strip() or p.suffix.lower().lstrip(".") not in extrakt.OHNE_OCR or not p.is_file():
            return t
        # Leseversuch merken: ohne Text nicht bei jedem Aufruf neu lesen (erst bei neuer Leser-Version)
        merker = config.DATA_DIR / "vorschau" / f"{int(d['id'])}.gelesen"
        version = str(extrakt.LESER_VERSION)
        try:
            if merker.read_text().strip() == version:
                return t
        except OSError:
            pass
        with tempfile.TemporaryDirectory() as arbeit:
            erg = extrakt.lies(p, Path(arbeit))
        if erg.text.strip() and db.text_nachtragen(d["id"], d["sha256"], erg.text):
            log.info("dok=%d text nachgeholt zeichen=%d", d["id"], len(erg.text))
            return erg.text
        try:
            merker.parent.mkdir(parents=True, exist_ok=True)
            merker.write_text(version)
        except OSError:
            pass
        return t

    @app.get("/vorschau/{dok_id}")
    def vorschau_bild(dok_id: int):
        d = db.hole(dok_id)
        if not d:
            return Response(status_code=404)
        bild = vorschau.bild(dok_id, datei_von(d), config.DATA_DIR / "vorschau", text_holen=lambda: text_von(d))
        if not bild:
            return Response(status_code=404)
        if bild.suffix == ".svg":
            # Platzhalter nicht zwischenspeichern: kommt später Text dazu, soll die Kachel ihn gleich zeigen
            return FileResponse(bild, media_type="image/svg+xml",
                                headers={"Cache-Control": "no-cache" if vorschau.ist_platzhalter(bild)
                                         else "private, max-age=86400",
                                         "Content-Security-Policy": "default-src 'none'; sandbox"})
        return FileResponse(bild, media_type="image/png",
                            headers={"Cache-Control": "private, max-age=86400"})

    def vorschau_kontext(d: dict) -> dict:
        """PDF im Rahmen; alles andere als Text bzw. CSV-Tabelle direkt in der Seite."""
        endung = Path(d["rel_pfad"]).suffix.lower().lstrip(".")
        if endung == "pdf":
            return {"ist_pdf": True}
        return {"ist_pdf": False, **vorschau.ansicht(endung, text_von(d))}

    # ── Dokument ─────────────────────────────────────────────────────────
    @app.get("/dokument/{dok_id}")
    def dokument(request: Request, dok_id: int):
        d = db.hole(dok_id)
        if not d:
            return seite(request, "fehler.html", 404, meldung="Dokument nicht gefunden.")
        pruefen = request.query_params.get("pruefen") == "1"
        naechstes = db.naechster_unsicher(d["datum"], d["id"], ausser=d["id"]) if pruefen else None
        return seite(request, "dokument.html", d=d, arten=config.lade_einstellungen()["arten"],
                     absender_liste=db.absender_liste(), **vorschau_kontext(d),
                     pruefen=pruefen, naechstes=naechstes, offen=db.zahlen()["unsicher"] if pruefen else 0,
                     dubletten=db.dubletten_zu(d["id"]))

    def weiter_pruefen(d: dict | None, gespeichert: bool) -> RedirectResponse:
        """Prüfmodus: gleich das nächste unsichere Dokument öffnen, sonst zurück zur Liste."""
        n = db.naechster_unsicher(d["datum"], d["id"], ausser=d["id"]) if d else db.naechster_unsicher(None, None)
        if n:
            return RedirectResponse(f"/dokument/{n}?pruefen=1" + ("&gespeichert=1" if gespeichert else ""),
                                    status_code=303)
        return RedirectResponse("/dokumente?unsicher=1&fertig=1", status_code=303)

    @app.post("/dokument/{dok_id}")
    def korrigieren(request: Request, dok_id: int, art: str = Form(...), absender: str = Form(...),
                    datum: str = Form(...), titel: str = Form(...), pruefen: str = Form("")):
        arten = config.lade_einstellungen()["arten"]
        dt = _datum(datum)
        absender = " ".join(absender.split())[:50]
        titel = " ".join(titel.split())[:60]
        if art not in arten or not dt or not absender or not titel:
            d = db.hole(dok_id)
            if not d:
                return seite(request, "fehler.html", 404, meldung="Dokument nicht gefunden.")
            return seite(request, "dokument.html", 400, d=d, arten=arten, absender_liste=db.absender_liste(),
                         **vorschau_kontext(d), fehler="Bitte Art, Absender, Datum und Titel angeben.")
        try:
            worker.korrigieren(dok_id, art, absender, dt, titel)
        except KeyError:
            return seite(request, "fehler.html", 404, meldung="Dokument nicht gefunden.")
        except OSError as e:
            log.warning("dok=%d korrektur fehler=%s", dok_id, type(e).__name__)
            return seite(request, "fehler.html", 409, meldung=DATEI_WEG)
        if pruefen == "1":
            return weiter_pruefen(db.hole(dok_id), gespeichert=True)
        return RedirectResponse(f"/dokument/{dok_id}?gespeichert=1", status_code=303)

    @app.post("/dokument/{dok_id}/papierkorb")
    def papierkorb(request: Request, dok_id: int, pruefen: str = Form("")):
        d = db.hole(dok_id)
        try:
            worker.in_papierkorb(dok_id)
        except OSError as e:
            log.warning("dok=%d papierkorb fehler=%s", dok_id, type(e).__name__)
            return seite(request, "fehler.html", 409, meldung=DATEI_WEG)
        if pruefen == "1":
            return weiter_pruefen(d, gespeichert=False)
        return RedirectResponse("/dokumente", status_code=303)

    @app.post("/dokument/{dok_id}/wiederherstellen")
    def wiederherstellen(request: Request, dok_id: int):
        try:
            worker.wiederherstellen(dok_id)
        except OSError as e:
            log.warning("dok=%d wiederherstellen fehler=%s", dok_id, type(e).__name__)
            return seite(request, "fehler.html", 409, meldung=DATEI_WEG)
        return RedirectResponse(f"/dokument/{dok_id}", status_code=303)

    @app.get("/datei/{dok_id}")
    def datei(dok_id: int, herunterladen: int = 0):
        d = db.hole(dok_id)
        if not d:
            return Response(status_code=404)
        if d["status"] == "papierkorb":
            p = Path(d["papierkorb_pfad"])
        else:
            p = (archiv / d["rel_pfad"]).resolve()
            if archiv.resolve() not in p.parents:
                return Response(status_code=404)
        if not p.is_file():
            return Response(status_code=404)
        name = Path(d["rel_pfad"]).name
        typ = INLINE_TYPEN.get(Path(name).suffix.lower())
        if typ and not herunterladen:
            kopf = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"}
            if typ != "application/pdf":
                kopf["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
            return FileResponse(p, media_type=typ, headers=kopf)
        return FileResponse(p, media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Content-Security-Policy": "default-src 'none'; sandbox"})

    # ── Einstellungen, Import, Bericht ───────────────────────────────────
    def import_lage() -> dict:
        """Der gewählte Import-Ordner: Anzeigepfad, ob verfügbar, wie viele Dateien, wie viele schon abgelegt."""
        ordner = config.lade_einstellungen()["import_ordner"]
        da = worker.quellen.aufloesen(ordner) is not None
        n, ganz = worker.quellen.zaehlen(ordner) if da else (0, True)
        return {"ordner": ordner, "anzeige": worker.quellen.anzeige_pfad(ordner), "da": da,
                "dateien": n, "dateien_text": f"{n:,}".replace(",", ".") + ("" if ganz else "+")
                + (" Datei" if n == 1 and ganz else " Dateien"),
                "kopien": db.anzahl_import(ordner)}

    @app.get("/einstellungen")
    def einstellungen(request: Request):
        return seite(request, "einstellungen.html", e=config.lade_einstellungen(), pfade=config.ANZEIGE_PFADE,
                     sicherungen=sicherung.liste(sicherungen_dir()), sich=dict(sich), leer=db.ist_leer(),
                     regeln=db.regeln_liste(), lauf=worker.status(), schwellen=SCHWELLEN_TEXT,
                     imp=import_lage(), gespeichert=request.query_params.get("gespeichert"))

    @app.post("/einstellungen")
    def einstellungen_speichern(unsicher_schwelle: float = Form(0.75)):
        e = config.lade_einstellungen()
        e["unsicher_schwelle"] = unsicher_schwelle
        config.speichere_einstellungen(e)
        return RedirectResponse("/einstellungen?gespeichert=1", status_code=303)

    @app.get("/api/ordner")
    def api_ordner(pfad: str = ""):
        rel = ordnerwahl.normalisiere(pfad)
        liste = worker.quellen.unterordner(rel) if rel is not None else None
        if liste is None:
            return JSONResponse({"fehler": "Diesen Ordner gibt es nicht (mehr)."}, status_code=404)
        n, ganz = worker.quellen.zaehlen(rel)
        teile, bisher = [], []
        for t in (rel.split("/") if rel else []):
            bisher.append(t)
            teile.append({"name": t, "pfad": "/".join(bisher)})
        return {"pfad": rel, "anzeige": worker.quellen.anzeige_pfad(rel), "teile": teile, "ordner": liste,
                "dateien": n, "ganz": ganz}

    @app.post("/import/ordner")
    def import_ordner_waehlen(request: Request, pfad: str = Form("")):
        rel = ordnerwahl.normalisiere(pfad)
        if rel is None or worker.quellen.aufloesen(rel) is None:
            return seite(request, "fehler.html", 400, meldung="Diesen Ordner kann die App nicht lesen – "
                         "Eingang und Archiv sind ausgenommen.")
        e = config.lade_einstellungen()
        e["import_ordner"] = rel
        config.speichere_einstellungen(e)
        return RedirectResponse("/einstellungen#import", status_code=303)

    @app.post("/import")
    def import_starten(request: Request):
        try:
            worker.import_starten(config.lade_einstellungen()["import_ordner"])
        except FileNotFoundError:
            return seite(request, "fehler.html", 409, meldung="Der gewählte Import-Ordner ist nicht mehr da. "
                         "Bitte in den Einstellungen einen anderen wählen.")
        return RedirectResponse("/", status_code=303)

    # ── Sicherung & Wiederherstellung (eine Datei mit allem) ────────────
    sich = {"laeuft": False, "art": "", "meldung": "", "fehler": False}
    sich_lock = threading.Lock()
    # Während eine Sicherung geschrieben wird, verschiebt der zweite Blick nichts (sonst passt die DB nicht zu den Dateien).
    worker.blick_aus = lambda: sich["laeuft"]

    def sicherungen_dir() -> Path:
        return config.DATA_DIR / "sicherungen"

    def im_hintergrund(art: str, arbeit) -> bool:
        with sich_lock:
            if sich["laeuft"]:
                return False
            sich.update(laeuft=True, art=art, meldung="", fehler=False)

        def lauf():
            try:
                sich["meldung"] = arbeit()
            except sicherung.SicherungFehler as e:
                sich.update(meldung=str(e), fehler=True)
            except Exception as e:
                log.error("sicherung %s fehler=%s", art, type(e).__name__)
                sich.update(meldung=f"Unerwarteter Fehler ({type(e).__name__}).", fehler=True)
            finally:
                sich["laeuft"] = False
        app.state.sicherung_thread = threading.Thread(target=lauf, name=f"sicherung-{art}", daemon=True)
        app.state.sicherung_thread.start()
        return True

    @app.post("/sicherung/erstellen")
    def sicherung_erstellen():
        def arbeit():
            p = sicherung.erstellen(db, archiv, config.DATA_DIR, sicherungen_dir(), config.lade_einstellungen())
            return f"Sicherung erstellt: {p.name}"
        im_hintergrund("erstellen", arbeit)
        return RedirectResponse("/einstellungen#sicherung", status_code=303)

    @app.get("/sicherung/datei/{name}")
    def sicherung_datei(name: str):
        p = sicherung.pfad_fuer(sicherungen_dir(), name)
        if not p:
            return Response(status_code=404)
        return FileResponse(p, media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.name)}",
            "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.post("/sicherung/loeschen")
    def sicherung_loeschen(name: str = Form("")):
        p = sicherung.pfad_fuer(sicherungen_dir(), name)
        if p:
            p.unlink(missing_ok=True)
        return RedirectResponse("/einstellungen#sicherung", status_code=303)

    @app.post("/sicherung/hochladen")
    async def sicherung_hochladen(datei: UploadFile):
        ziel = sicherungen_dir()
        ziel.mkdir(parents=True, exist_ok=True)
        teil = ziel / f".hochladen-{os.getpid()}-{int(time.time())}.teil"
        try:
            with open(teil, "wb") as out:
                while block := await datei.read(1 << 20):
                    out.write(block)
            name = f"fah-ablage-sicherung-hochgeladen-{time.strftime('%Y%m%d-%H%M%S')}.tar"
            os.replace(teil, ziel / name)
        finally:
            teil.unlink(missing_ok=True)
        return RedirectResponse("/einstellungen#sicherung", status_code=303)

    @app.post("/sicherung/wiederherstellen")
    def sicherung_wiederherstellen(request: Request, name: str = Form("")):
        p = sicherung.pfad_fuer(sicherungen_dir(), name)
        if not p:
            return seite(request, "fehler.html", 404, meldung="Diese Sicherung gibt es nicht.")
        if not db.ist_leer():
            return seite(request, "fehler.html", 409, meldung="Wiederherstellen geht nur in eine leere Ablage "
                         "(frische Installation). Es sind schon Dokumente vorhanden.")

        def arbeit():
            with worker._lock:
                e = sicherung.wiederherstellen(p, db, archiv, config.DATA_DIR, config.speichere_einstellungen)
            db.ordner_nachtragen(config.STANDARD_IMPORT_ORDNER, str(config.QUELLEN_DIR))
            worker.titel_vereinheitlichen()
            return f"Wiederhergestellt: {e['dokumente']} Dokumente, {e['dateien']} Dateien."
        im_hintergrund("wiederherstellen", arbeit)
        return RedirectResponse("/einstellungen#sicherung", status_code=303)

    # ── Warteschlange und doppelte Dateien ───────────────────────────────
    QUELLEN_NAMEN = {"eingang": "Eingang", "upload": "Upload", "import": "Import"}

    @app.get("/warteschlange")
    def warteschlange(request: Request):
        lauf = worker.status()
        auftraege = db.warteschlange_liste(300)
        jetzt = time.time()
        for a in auftraege:
            a["name"] = Path(a["pfad"]).name
            a["wo"] = worker._herkunft_anzeige(a, Path(a["pfad"]))
            a["spaeter"] = a["naechster_ts"] > jetzt
        zaehlung = db.warteschlange_zaehlung()
        importe = [{"ordner": g["ordner"], "anzeige": worker.quellen.anzeige_pfad(g["ordner"]), "n": g["n"]}
                   for g in zaehlung["gruppen"] if g["quelle"] == "import"]
        aktuell_id = (lauf["aktuell"] or {}).get("id") if (lauf["aktuell"] or {}).get("quelle") != "blick" else None
        return seite(request, "warteschlange.html", auftraege=auftraege, gesamt=zaehlung["gesamt"], importe=importe,
                     lauf=lauf, aktuell_id=aktuell_id, quellen_namen=QUELLEN_NAMEN)

    @app.post("/warteschlange/import-abbrechen")
    def import_abbrechen(ordner: str = Form("")):
        rel = ordnerwahl.normalisiere(ordner)
        if rel is not None:
            worker.import_abbrechen(rel)
        return RedirectResponse("/warteschlange", status_code=303)

    def dubletten_dir() -> Path:
        return worker.daten / "dubletten"

    def dubletten_datei(eintrag: dict | None) -> Path | None:
        """Aufbewahrte Kopie – nur direkt im Dubletten-Ordner, nie ein Link."""
        if not eintrag or not eintrag["datei"]:
            return None
        p = Path(eintrag["datei"])
        try:
            if p.is_symlink() or not p.is_file() or p.resolve().parent != dubletten_dir().resolve():
                return None
        except OSError:
            return None
        return p

    @app.get("/doppelte")
    def doppelte(request: Request):
        gruppen = db.dubletten_gruppen()
        for g in gruppen:
            for f in g["dateien"]:
                f["kopie"] = dubletten_datei(f) is not None
        return seite(request, "doppelte.html", gruppen=gruppen, gezaehlt=db.zaehler("dubletten"),
                     quellen_namen=QUELLEN_NAMEN)

    @app.get("/doppelte/{dub_id}/datei")
    def doppelte_datei(dub_id: int):
        e = db.dublette(dub_id)
        p = dubletten_datei(e)
        if not p:
            return Response(status_code=404)
        name = p.name if e["nachgetragen"] else e["name"]
        return FileResponse(p, media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.post("/doppelte/{dub_id}/loeschen")
    def doppelte_loeschen(dub_id: int):
        e = db.dublette(dub_id)
        if e:
            if p := dubletten_datei(e):
                p.unlink(missing_ok=True)
            db.dublette_loeschen(dub_id)       # Import-Originale bleiben immer, wo sie sind
        return RedirectResponse("/doppelte", status_code=303)

    @app.post("/blick/wiederholen")
    def blick_wiederholen():
        worker.blick_wiederholen()
        return RedirectResponse("/einstellungen?gespeichert=1#blick", status_code=303)

    @app.post("/import/zuruecksetzen")
    def import_zuruecksetzen():
        worker.import_zuruecksetzen(config.lade_einstellungen()["import_ordner"])
        return RedirectResponse("/einstellungen?gespeichert=1#import", status_code=303)

    @app.get("/bericht")
    def bericht(request: Request):
        alle = request.query_params.get("alle") == "1"
        ordner = None if alle else config.lade_einstellungen()["import_ordner"]
        gruppen = db.bericht(ordner)
        gesamt = sum(g["gesamt"] for g in gruppen)
        einheitlich = sum(g["einheitlich"] * g["gesamt"] for g in gruppen) / gesamt if gesamt else 0
        unsicher = sum(g["unsicher"] for g in gruppen) / gesamt if gesamt else 0
        return seite(request, "bericht.html", gruppen=gruppen, gesamt=gesamt, einheitlich=einheitlich,
                     unsicher=unsicher, lauf=worker.status(), alle=alle,
                     ordner_anzeige=worker.quellen.anzeige_pfad(ordner or ""))

    return app


app = erstelle_app() if os.environ.get("ABLAGE_KEIN_AUTOSTART") != "1" else None
