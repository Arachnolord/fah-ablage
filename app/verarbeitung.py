# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Hintergrund-Arbeiter: Eingang beobachten, Dokumente lesen, einordnen, ablegen.

Ein Dokument nach dem anderen, Eingang/Upload vor Import. Verarbeitet wird immer
eine KOPIE; das Original im Eingang wird erst nach dem Eintrag in die Datenbank
gelöscht – und nur, wenn es sich währenddessen nicht verändert hat.
Logs enthalten nur IDs und Zahlen – nie Text, Dateinamen oder Absender.
"""
import hashlib
import logging
import os
import re
import shutil
import stat
import threading
import time
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

import ablage
import config
import extrakt
import ki
import ordnerwahl
import titel as titelregeln
import vorschau
import zweiter_blick as zb
from datum import finde_briefdatum, finde_zeitraum
from regeln import PLATZHALTER, Regeln

log = logging.getLogger("ablage")

SCAN_ABSTAND = 10.0
RUHE = 60.0                  # so lange muss eine Eingangsdatei unverändert sein
WARTEN = (120, 600, 1800)    # Wartezeit nach dem 1./2./3. Versuch
MAX_KI_VERSUCHE = 3          # unbrauchbare KI-Antworten, danach Sonstiges/Unbekannt
MAX_FEHLER = 3               # unerwartete Fehler, danach Quarantäne
MAX_UNVOLLSTAENDIG = 2       # PDF ohne Ende: so oft später erneut versuchen
KI_PAUSE = 60.0
PAPIERKORB_TAGE = 30
MIN_TEXT = 40
BLICK_ABSTAND = 20.0         # Nachbarsuche für unsichere Dokumente höchstens so oft (Sekunden)
MAX_BLICK_VERSUCHE = 3       # so oft darf die KI in der zweiten Runde für ein Dokument ausfallen


def _ignorieren(p: Path) -> bool:
    return ordnerwahl.datei_ignorieren(p.name)


def _sicher_kopieren(quelle: Path, ziel: Path) -> bool:
    """Nur eine echte Datei kopieren – keinen Link (O_NOFOLLOW), kein Gerät, keine Pipe. Gelesen wird vom
    geöffneten Handle, damit zwischen Prüfen und Lesen nichts mehr ausgetauscht werden kann."""
    try:
        fd = os.open(quelle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as q:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        with open(ziel, "wb") as z:
            shutil.copyfileobj(q, z, 1 << 20)
    return True


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _stand(p: Path):
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _pdf_unvollstaendig(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            if f.read(5) != b"%PDF-":
                return False
            f.seek(max(0, p.stat().st_size - 2048))
            return b"%%EOF" not in f.read()
    except OSError:
        return False


def _ohne_nummer(rel: PurePosixPath) -> PurePosixPath:
    return rel.with_name(re.sub(r" \(\d+\)$", "", rel.stem) + rel.suffix)


class Worker:
    def __init__(self, db, einstellungen_fn, ki_fn=ki.ordne_ein, lies_fn=extrakt.lies,
                 eingang=None, archiv=None, quellen=None, daten=None, ki_url=None,
                 uhr=time.time, heute=date.today, ki_ok_fn=None):
        self.db = db
        self.einstellungen = einstellungen_fn
        self.ki_fn, self.lies_fn = ki_fn, lies_fn
        self.eingang = Path(eingang or config.EINGANG_DIR)
        self.archiv = Path(archiv or config.ARCHIV_DIR)
        self.daten = Path(daten or config.DATA_DIR)
        if not isinstance(quellen, ordnerwahl.Quellen):
            pfade = config.ANZEIGE_PFADE
            quellen = ordnerwahl.Quellen(
                quellen or config.QUELLEN_DIR, sperr_ordner=(self.eingang, self.archiv, self.daten),
                sperr_rel=[r for r in (config.relativ_zur_quelle(pfade["eingang"]),
                                       config.relativ_zur_quelle(pfade["archiv"])) if r],
                anzeige=pfade["quellen"], sperr_anzeige=(pfade["eingang"], pfade["archiv"]))
        self.quellen = quellen
        self.ki_url = ki_url or config.KI_URL
        self.ki_ok_fn = ki_ok_fn or (lambda: ki.erreichbar(self.ki_url))
        self.uhr, self.heute = uhr, heute
        self.regeln = Regeln(db)
        self.arbeit = self.daten / "arbeit"
        self.vorschau_dir = self.daten / "vorschau"
        for d in (self.arbeit, self.daten / "dubletten", self.daten / "fehler", self.daten / "papierkorb"):
            d.mkdir(parents=True, exist_ok=True)
        self._gesehen: dict[str, tuple] = {}
        self._aktuell: dict | None = None
        self._pause_bis = 0.0
        self._letzte_nachbarsuche = -BLICK_ABSTAND
        self._blick_versuche: dict[int, int] = {}
        self.blick_aus = lambda: False       # der Server schaltet den zweiten Blick während einer Sicherung ab
        self._stop = threading.Event()
        self._wecker = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()   # nur ums Ablegen/Verschieben, nie um OCR oder KI
        self._import_lock = threading.Lock()   # Import starten/zurücksetzen nie gleichzeitig (Doppelklick)

    # ── Lebenszyklus ─────────────────────────────────────────────────────
    def start(self) -> None:
        self._aufraeumen_arbeit()
        ablage.reste_entfernen(self.archiv)
        self._thread = threading.Thread(target=self._schleife, name="ablage-worker", daemon=True)
        self._thread.start()
        # Das einmalige Nachrechnen der Doppelten liest viele Dateien – eigener Faden, der Arbeiter läuft weiter.
        threading.Thread(target=self._dubletten_einmalig, name="ablage-dubletten", daemon=True).start()

    def _dubletten_einmalig(self) -> None:
        try:
            self.dubletten_nachtragen()
        except Exception as e:
            log.error("dubletten fehler=%s", type(e).__name__)

    def stop(self) -> None:
        self._stop.set()
        self._wecker.set()
        if self._thread:
            self._thread.join(timeout=50)

    def wecken(self) -> None:
        self._wecker.set()

    def _schleife(self) -> None:
        letzter_scan, letzter_korb = 0.0, 0.0
        try:
            self.titel_vereinheitlichen()
        except Exception as e:
            log.error("titel fehler=%s", type(e).__name__)
        while not self._stop.is_set():
            try:
                jetzt = self.uhr()
                if jetzt - letzter_scan >= SCAN_ABSTAND:
                    self.scan_eingang()
                    letzter_scan = jetzt
                if jetzt - letzter_korb >= 86400:
                    self.papierkorb_aufraeumen()
                    letzter_korb = jetzt
                gearbeitet = self.schritt() or self.blick_schritt()
            except Exception as e:  # der Arbeiter darf nie sterben
                log.error("worker fehler=%s", type(e).__name__)
                gearbeitet = False
            if not gearbeitet:
                self._wecker.wait(2.0)
                self._wecker.clear()

    def _aufraeumen_arbeit(self) -> None:
        for p in self.arbeit.iterdir():
            try:
                p.unlink() if p.is_file() else shutil.rmtree(p)
            except OSError:
                pass

    # ── Eingang / Import ─────────────────────────────────────────────────
    def scan_eingang(self) -> int:
        neu, gesehen, jetzt = 0, {}, self.uhr()
        if not self.eingang.is_dir():
            return 0
        for p in self.eingang.rglob("*"):
            if p.is_symlink() or not p.is_file() or any(_ignorieren(Path(t)) for t in p.relative_to(self.eingang).parts):
                continue
            stand = _stand(p)
            if not stand:
                continue
            k = str(p)
            vorher = self._gesehen.get(k)
            seit = vorher[1] if vorher and vorher[0] == stand else jetzt
            gesehen[k] = (stand, seit)
            if jetzt - seit >= RUHE and stand[0] > 0 and not self.db.in_warteschlange(k):
                if self.db.einreihen(k, "eingang"):
                    neu += 1
        self._gesehen = gesehen
        if neu:
            self.wecken()
        return neu

    def import_starten(self, ordner: str) -> int:
        """Alle Dateien des gewählten Ordners (relativ zur Quellen-Wurzel) einreihen. Nur lesend."""
        ordner = ordnerwahl.normalisiere(ordner)
        if ordner is None or self.quellen.aufloesen(ordner) is None:
            raise FileNotFoundError("Ordner nicht verfügbar")
        with self._import_lock:
            lief_schon = self.db.anzahl("import") > 0
            n = self.db.einreihen_viele([(str(p), "import", herkunft, ordner)
                                         for p, herkunft in self.quellen.dateien(ordner)])
            if lief_schon:      # läuft noch ein Import, kommt der neue Ordner hinten dran
                self.db.zaehle("import_gesamt", n)
            else:
                self.db.setze_zaehler("import_gesamt", n)
                self.db.setze_zaehler("import_fertig", 0)
        self.wecken()
        log.info("import eingereiht dok=%d", n)
        return n

    def import_abbrechen(self, ordner: str) -> int:
        """Noch wartende Dateien dieses Import-Ordners aus der Warteschlange nehmen; Abgelegtes bleibt."""
        with self._import_lock:
            weg = self.db.warteschlange_leeren("import", ordner)
            if self.db.anzahl("import") == 0:
                self.db.setze_zaehler("import_gesamt", 0)
                self.db.setze_zaehler("import_fertig", 0)
            elif weg:
                self.db.zaehle("import_gesamt", -weg)
        log.info("import abgebrochen dok=%d", weg)
        return weg

    def import_zuruecksetzen(self, ordner: str | None = None) -> int:
        """Aus diesem Import-Ordner (None: aus allen) abgelegte KOPIEN entfernen; die Originale bleiben."""
        with self._import_lock, self._lock:
            entfernt = self.db.warteschlange_leeren("import", ordner)
            n = 0
            for d in self.db.alle_aus_quelle("import", ordner):
                p = Path(d["papierkorb_pfad"]) if d["status"] == "papierkorb" else self.archiv / d["rel_pfad"]
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    continue
                if d["status"] != "papierkorb":
                    ablage.leere_ordner_entfernen(p.parent, self.archiv)
                vorschau.loeschen(d["id"], self.vorschau_dir)
                self.db.loeschen(d["id"])
                n += 1
            if self.db.anzahl("import") == 0:
                self.db.setze_zaehler("import_gesamt", 0)
                self.db.setze_zaehler("import_fertig", 0)
            elif entfernt:
                self.db.zaehle("import_gesamt", -entfernt)
        log.info("import zurueckgesetzt dok=%d", n)
        return n

    def status(self) -> dict:
        return {
            "laeuft": bool(self._thread and self._thread.is_alive()),
            "aktuell": dict(self._aktuell) if self._aktuell else None,
            "ki_pause": self._pause_bis > self.uhr(),
            "warteschlange": self.db.anzahl(),
            "import_offen": self.db.anzahl("import"),
            "import_gesamt": self.db.zaehler("import_gesamt"),
            "import_fertig": self.db.zaehler("import_fertig"),
            **{f"blick_{k}": v for k, v in self.db.blick_zahlen().items()},
        }

    # ── Ein Dokument ─────────────────────────────────────────────────────
    def schritt(self) -> bool:
        if self.uhr() < self._pause_bis:
            return False
        job = self.db.naechster(self.uhr())
        if not job:
            return False
        if not self.ki_ok_fn():
            self._pause_bis = self.uhr() + KI_PAUSE
            log.warning("ki=nicht_erreichbar pause=%ds", int(KI_PAUSE))
            return False
        # Versuch VORHER zählen: stirbt der Prozess mitten drin, läuft der Auftrag nicht endlos.
        self.db.fehlschlag(job["id"], self.uhr() + WARTEN[min(job["versuche"], 2)])
        self._aktuell = {"id": job["id"], "quelle": job["quelle"], "seit": self.uhr()}
        try:
            self._verarbeite(job)
        except Exception as e:
            self._unerwartet(job, e)
        finally:
            self._aktuell = None
        return True

    def _zurueck(self, job: dict, warten: float) -> None:
        """Auftrag ohne Zählung zurückstellen (kein Fehler des Dokuments)."""
        self.db.setze_auftrag(job["id"], versuche=job["versuche"], naechster_ts=self.uhr() + warten)
        self._aufraeumen_arbeit()

    def _unerwartet(self, job: dict, e: Exception) -> None:
        log.error("auftrag=%d fehler=%s versuch=%d", job["id"], type(e).__name__, job["versuche"] + 1)
        if job["versuche"] + 1 >= MAX_FEHLER:
            quelle = Path(job["pfad"])
            if job["quelle"] != "import" and quelle.exists():
                ziel = self.daten / "fehler" / f"{int(self.uhr())}-{job['id']}{quelle.suffix.lower()[:10]}"
                try:
                    shutil.move(str(quelle), str(ziel))
                except OSError as e2:
                    log.error("auftrag=%d quarantaene fehler=%s", job["id"], type(e2).__name__)
            self._erledigt(job)
            self.db.zaehle("fehler")
        self._aufraeumen_arbeit()

    def _verarbeite(self, job: dict) -> None:
        start = time.monotonic()
        quelle = Path(job["pfad"])
        eingang = job["quelle"] != "import"
        stand = _stand(quelle) if self._darf_lesen(job, quelle) else None
        datei = self.arbeit / f"{job['id']}{quelle.suffix.lower()[:10]}"
        if not stand or not _sicher_kopieren(quelle, datei):
            self._weg(job)              # verschwunden, verschoben oder keine normale Datei mehr
            return
        if eingang:
            if _stand(quelle) != stand:            # wird noch geschrieben
                self._zurueck(job, RUHE)
                return
            if _pdf_unvollstaendig(datei) and job["versuche"] < MAX_UNVOLLSTAENDIG:
                self._aufraeumen_arbeit()          # Versuch zählt, später noch einmal
                log.info("auftrag=%d pdf_unvollstaendig versuch=%d", job["id"], job["versuche"] + 1)
                return
        sha = _sha256(datei)
        if vorhanden := self.db.gibt_sha(sha):
            self._dublette(job, quelle, stand, sha, vorhanden)
            return

        e = self.einstellungen()
        arten = e["arten"]
        erg = self.lies_fn(datei, self.arbeit)
        gruende, ki_ausfall = [], False
        try:
            einordnung = self.ki_fn(erg.text, quelle.name, arten, self.db.absender_liste(), self.ki_url,
                                    hinweise=e.get("art_hinweise") or {})
        except ki.KiNichtErreichbar:
            self._pause_bis = self.uhr() + KI_PAUSE
            self._zurueck(job, KI_PAUSE)
            log.warning("auftrag=%d ki=abgebrochen pause=%ds", job["id"], int(KI_PAUSE))
            return
        except ki.KiFehler:
            if job["versuche"] + 1 < MAX_KI_VERSUCHE + 1:
                self._aufraeumen_arbeit()
                log.warning("auftrag=%d ki=unbrauchbar versuch=%d", job["id"], job["versuche"] + 1)
                return
            einordnung = ki.Einordnung("Sonstiges", "Unbekannt", None, "Dokument", 0.0)
            gruende.append("KI-Antwort unbrauchbar")
            ki_ausfall = True

        if ki_ausfall:
            absender, art, sicher = "Unbekannt", "Sonstiges", 0.0
        else:
            absender = self.regeln.kanon(einordnung.absender) or "Unbekannt"
            regel_art = self.regeln.art_fuer(absender)
            art = einordnung.art if einordnung.art in arten else "Sonstiges"
            sicher = einordnung.sicherheit
            if regel_art in arten:
                if regel_art != art:
                    gruende.append("Regel weicht von KI ab")
                art, sicher = regel_art, 1.0
            elif sicher < e["unsicher_schwelle"]:
                gruende.append("KI unsicher")
            if art == "Sonstiges" and not gruende:
                gruende.append("keine passende Art")
            if regel_art not in arten:
                frueher = self.db.art_verteilung(absender)
                if sum(frueher.values()) >= 2 and frueher.most_common(1)[0][0] != art:
                    gruende.append("anders als frühere Briefe dieses Absenders")
        if len("".join((erg.text or "").split())) < MIN_TEXT and not erg.fehler:
            gruende.append("kaum Text lesbar")
        datum = einordnung.datum or finde_briefdatum(erg.text, self.heute()) or finde_zeitraum(erg.text, self.heute())
        if not datum:
            datum = date.fromtimestamp(os.stat(quelle).st_mtime if not eingang else self.uhr())
            gruende.append("kein Datum erkannt")
        if erg.fehler:
            gruende.append(erg.fehler)
        titel = einordnung.titel or "Dokument"
        titel = titelregeln.gehaltsabrechnung(titel, erg.text, datum, self.heute()) or titel

        rel = ablage.zielpfad(art, absender, datum, titel, erg.endung)
        ms = int((time.monotonic() - start) * 1000)
        with self._lock:
            if not self.db.hole_auftrag(job["id"]):     # währenddessen zurückgesetzt: Arbeit verwerfen
                self._aufraeumen_arbeit()
                return
            neu = ablage.ablegen(erg.ablage_datei, rel, self.archiv)
            dok = self.db.einfuegen(
                text=erg.text, sha256=sha, rel_pfad=str(neu), art=art, absender=absender,
                datum=datum.isoformat(), titel=titel, unsicher=1 if gruende else 0, grund="; ".join(gruende),
                erst_grund="; ".join(gruende), quelle_datei=f"{self._herkunft_anzeige(job, quelle)}/{quelle.name}",
                quelle=job["quelle"], herkunft=job["herkunft"], ordner=job.get("ordner") or "",
                eingang_ts=self.uhr(), sicherheit=sicher, ms=ms)
            self._erledigt(job)
            if not gruende:
                self.db.zaehle("wissen")       # ein sicheres Dokument mehr: unsichere neu vergleichen
        if eingang:
            if _stand(quelle) == stand:
                quelle.unlink(missing_ok=True)
            else:
                log.warning("dok=%d quelle_geaendert bleibt_im_eingang", dok)
        self._aufraeumen_arbeit()
        log.info("dok=%d art_idx=%d sicher=%.2f unsicher=%d ocr=%d ms=%d",
                 dok, arten.index(art) if art in arten else -1, sicher, 1 if gruende else 0, int(erg.ocr), ms)

    def _darf_lesen(self, job: dict, quelle: Path) -> bool:
        if job["quelle"] == "import":
            return self.quellen.darf_lesen(job.get("ordner") or "", job["herkunft"], quelle)
        try:      # Eingang/Upload: auch über verlinkte Unterordner nie aus dem Eingang heraus
            return Path(os.path.realpath(quelle.parent)).is_relative_to(os.path.realpath(self.eingang))
        except OSError:
            return False

    def _erledigt(self, job: dict) -> None:
        """Auftrag austragen; „Import x von y“ zählt nur, was wirklich noch in der Warteschlange stand."""
        if self.db.entfernen(job["id"]) and job["quelle"] == "import":
            self.db.zaehle("import_fertig")

    def _weg(self, job: dict) -> None:
        self._erledigt(job)
        self._aufraeumen_arbeit()

    def _herkunft_anzeige(self, job: dict, quelle: Path) -> str:
        """Ordner, aus dem die Datei kam – so, wie der Betreiber ihn auf dem NAS sieht."""
        if job["quelle"] == "import":
            return self.quellen.anzeige_pfad("/".join(x for x in (job.get("ordner") or "", job["herkunft"]) if x))
        try:
            unter = quelle.parent.relative_to(self.eingang).as_posix()
        except ValueError:
            unter = ""
        basis = config.ANZEIGE_PFADE["eingang"] if job["quelle"] == "eingang" else "Upload"
        return f"{basis}/{unter}" if unter and unter != "." else basis

    def _ist_original(self, dok_id: int, pfad: str, name: str, ordner: str, herkunft: str) -> bool:
        """Ist diese Import-Datei die schon abgelegte selbst (Import erneut gestartet, Ordner überlappen)?"""
        d = self.db.hole(dok_id)
        if not d or d["quelle"] != "import":
            return False
        if d["quelle_datei"]:
            return d["quelle_datei"] == f"{pfad}/{name}"
        return d["ordner"] == ordner and d["herkunft"] == herkunft      # vor 1.8: Name nicht gespeichert

    def _dublette(self, job: dict, quelle: Path, stand, sha: str, dok_id: int) -> None:
        pfad, datei = self._herkunft_anzeige(job, quelle), ""
        if job["quelle"] == "import" and self._ist_original(dok_id, pfad, quelle.name, job.get("ordner") or "",
                                                             job["herkunft"]):
            if not self.db.hole(dok_id)["quelle_datei"]:
                self.db.aendern(dok_id, quelle_datei=f"{pfad}/{quelle.name}")   # jetzt wissen wir, woher es kam
            self._weg(job)
            return
        if job["quelle"] != "import" and _stand(quelle) == stand:
            ziel = self.daten / "dubletten" / f"{int(self.uhr())}-{job['id']}{quelle.suffix.lower()[:10]}"
            shutil.move(str(quelle), str(ziel))
            datei = str(ziel)
        self.db.dublette_merken(job["quelle"], quelle.name, pfad, dok_id, sha, datei, ts=self.uhr())
        self.db.zaehle("dubletten")
        self._weg(job)
        log.info("auftrag=%d dublette", job["id"])

    # ── Zweiter Blick auf unsichere Dokumente ────────────────────────────
    def blick_schritt(self) -> bool:
        """Leerlauf: Nachbarsuche (schnell, alle betroffenen), danach EIN Dokument in die genauere KI-Runde."""
        if self.blick_aus():                 # z. B. während eine Sicherung geschrieben wird
            return False
        if self.uhr() - self._letzte_nachbarsuche >= BLICK_ABSTAND:
            self._letzte_nachbarsuche = self.uhr()
            self.blick_nachbarn()
        if self.db.anzahl() or self.uhr() < self._pause_bis:
            return False
        dok_id = self.db.naechster_blick_ki()
        if dok_id is None:
            return False
        if not self.ki_ok_fn():
            self._pause_bis = self.uhr() + KI_PAUSE
            return False
        self.blick_ki(dok_id)
        return True

    def blick_wiederholen(self) -> int:
        """Alle unsicheren noch einmal durch beide Stufen schicken."""
        n = self.db.blick_zuruecksetzen()
        self._blick_versuche.clear()
        self._letzte_nachbarsuche = -BLICK_ABSTAND
        self.wecken()
        return n

    def _nachbarn(self, dok_id: int, w: frozenset) -> list[zb.Nachbar]:
        aus = []
        for k in self.db.nachbar_kandidaten(dok_id, zb.suchwoerter(w), zb.KANDIDATEN):
            aus.append(zb.Nachbar(zb.aehnlichkeit(w, zb.woerter(k["text"])), k["id"], k["art"], k["absender"],
                                  k["titel"]))
        return sorted(aus, key=lambda n: -n.aehnlich)

    def blick_nachbarn(self) -> int:
        """Stufe 1 für alle unsicheren, die seit ihrer letzten Prüfung neues Wissen sehen könnten."""
        stand = self.db.zaehler("wissen") + 1
        geklaert = 0
        for dok_id in self.db.fuer_blick(stand):
            if self._stop.is_set() or self.db.anzahl() or self.blick_aus():
                break                         # neue Dokumente gehen vor; der Rest kommt in der nächsten Runde
            try:
                geklaert += self._nachbarn_fuer(dok_id, stand)
            except Exception as e:            # ein kaputtes Dokument darf die übrigen nicht aushungern
                log.error("dok=%d zweiter_blick nachbarn fehler=%s", dok_id, type(e).__name__)
                self.db.aendern(dok_id, blick_stand=stand)
        if geklaert:
            log.info("zweiter_blick nachbarn geklaert=%d", geklaert)
        return geklaert

    def _nachbarn_fuer(self, dok_id: int, stand: int) -> int:
        d = self.db.hole(dok_id)
        if not d or d["unsicher"] != 1:
            return 0
        gruende = zb.gruende_von(d["grund"])
        text = self.db.text(dok_id)
        w = zb.woerter(text)
        urteil = None
        if len(w) >= zb.MIN_WOERTER and set(gruende) & zb.ART_GRUENDE:
            urteil = zb.urteil_nachbarn(self._nachbarn(dok_id, w))
        absender = d["absender"]
        if urteil and urteil.absender and zb.absender_wechsel_ok(absender, urteil.absender, text, self.regeln.kanon):
            absender = urteil.absender
        if urteil and (regel := self.regeln.art_fuer(absender)) and regel != urteil.art:
            urteil = None                     # eine vom Betreiber gelernte Regel überstimmen die Nachbarn nicht
        neues_datum = finde_zeitraum(text, self.heute()) if zb.DATUM_GRUND in gruende else None
        if not urteil and not neues_datum:
            self.db.aendern(dok_id, blick_stand=stand)
            return 0
        rest = [g for g in gruende if not ((urteil and g in zb.ART_GRUENDE) or (neues_datum and g == zb.DATUM_GRUND))]
        art = urteil.art if urteil else d["art"]
        if not urteil:
            absender = d["absender"]
        teile = []
        if urteil:
            teile.append(f"eingeordnet wie {urteil.n} fast gleiche, geprüfte Dokumente"
                         + (f" (Art geändert, vorher „{d['art']}“)" if art != d["art"] else ""))
        if neues_datum:
            teile.append("Datum aus dem Abrechnungsmonat")
        ok = self._blick_anwenden(d, art, absender, neues_datum or date.fromisoformat(d["datum"]), d["titel"], text,
                                  rest, "Zweiter Blick: " + ", ".join(teile), stand=stand)
        return int(ok and not rest)

    def blick_ki(self, dok_id: int) -> None:
        """Stufe 2: die KI noch einmal, mit mehr Text und ähnlichen geprüften Dokumenten als Vorbild."""
        d = self.db.hole(dok_id)
        if not d or d["unsicher"] != 1 or d["status"] != "abgelegt":
            return
        # Versuch VORHER zählen: antwortet die KI für genau dieses Dokument nie, blockiert es nicht ewig.
        versuch = self._blick_versuche.get(dok_id, 0) + 1
        self._blick_versuche[dok_id] = versuch
        if versuch > MAX_BLICK_VERSUCHE:
            self._blick_merken(d, blick=1, blick_text="Zweiter Blick: die KI hat nicht geantwortet")
            self._blick_versuche.pop(dok_id, None)
            return
        gruende = zb.gruende_von(d["grund"])
        text = self.db.text(dok_id)
        w = zb.woerter(text)
        nachbarn = self._nachbarn(dok_id, w) if len(w) >= zb.MIN_WOERTER else []
        beispiele = [(n.absender, n.art, n.titel) for n in nachbarn[:3] if n.aehnlich >= zb.SCHWACH]
        e = self.einstellungen()
        arten = e["arten"]
        self._aktuell = {"id": dok_id, "quelle": "blick", "seit": self.uhr()}
        try:
            ein = self.ki_fn(text, "dokument" + Path(d["rel_pfad"]).suffix.lower(), arten, self.db.absender_liste(),
                             self.ki_url, hinweise=e.get("art_hinweise") or {},
                             zweiter_blick={"gruende": gruende, "beispiele": beispiele})
        except ki.KiNichtErreichbar:
            self._pause_bis = self.uhr() + KI_PAUSE
            return
        except ki.KiFehler:
            self._blick_merken(d, blick=1, blick_text="Zweiter Blick: KI-Antwort unbrauchbar")
            self._blick_versuche.pop(dok_id, None)
            return
        finally:
            self._aktuell = None
        self._blick_versuche.pop(dok_id, None)
        ki_art = ein.art if ein.art in arten else "Sonstiges"
        ki_absender = self.regeln.kanon(ein.absender) or d["absender"]
        stuetzen = set()                      # nur aus geprüften Quellen, nie aus anderen unsicheren
        if m := zb.mehrheit_schwach(nachbarn):
            stuetzen.add(m)
        if (regel := self.regeln.art_fuer(ki_absender)) in arten:
            stuetzen.add(regel)
        frueher = self.db.art_verteilung_geprueft(ki_absender)
        if sum(frueher.values()) >= 2:
            top, k = frueher.most_common(1)[0]
            if k / sum(frueher.values()) >= 0.6:
                stuetzen.add(top)
        art_gruende = bool(set(gruende) & zb.ART_GRUENDE)
        art_klar = art_gruende and zb.urteil_ki(ki_art, d["art"], gruende, stuetzen,
                                                ki_sicher=ein.sicherheit >= e["unsicher_schwelle"])
        art = ki_art if art_klar else d["art"]
        absender = d["absender"]
        if art_klar and ki_absender.casefold() not in PLATZHALTER and zb.absender_wechsel_ok(
                d["absender"], ki_absender, text, self.regeln.kanon):
            absender = ki_absender
        datum, datum_klar = date.fromisoformat(d["datum"]), False
        if zb.DATUM_GRUND in gruende:
            # Ein KI-Datum zählt nur, wenn sein Jahr auch im Text steht und es nicht in der Zukunft liegt.
            kandidat = ein.datum if ein.datum and ein.datum <= self.heute() + timedelta(days=45) \
                and str(ein.datum.year) in (text or "") else None
            if neu := kandidat or finde_zeitraum(text, self.heute()):
                datum, datum_klar = neu, True
        titel = ein.titel if d["titel"] == "Dokument" and ein.titel else d["titel"]
        rest = [g for g in gruende if not ((art_klar and g in zb.ART_GRUENDE) or (datum_klar and g == zb.DATUM_GRUND))]
        teile = []
        if art_klar:
            teile.append(("Art bestätigt" if art == d["art"] else f"Art geändert (vorher „{d['art']}“)")
                         + " – gestützt durch " + ("ähnliche geprüfte Dokumente, Regeln oder frühere Briefe"
                                                  if ki_art in stuetzen else "eine zweite, gleiche Antwort"))
        elif art_gruende:
            teile.append(f"Art weiter unklar – die KI meint diesmal „{ki_art}“" if ki_art != d["art"]
                         else "Art weiter unklar")
        if datum_klar:
            teile.append("Datum gefunden")
        elif zb.DATUM_GRUND in gruende:
            teile.append("Datum weiter unklar")
        self._blick_anwenden(d, art, absender, datum, titel, text, rest,
                             "Zweiter Blick (KI, genauer): " + ", ".join(teile or ["nichts Neues"]), ki_runde=True)
        log.info("zweiter_blick ki dok=%d geklaert=%d", dok_id, int(not rest))

    def _unveraendert(self, jetzt: dict | None, d: dict) -> bool:
        """Ist das Dokument noch so, wie der zweite Blick es gelesen hat? (Sonst hat der Betreiber eingegriffen.)"""
        return bool(jetzt) and jetzt["status"] == "abgelegt" and jetzt["unsicher"] == 1 and all(
            jetzt[k] == d[k] for k in ("grund", "art", "absender", "datum", "titel", "rel_pfad"))

    def _blick_merken(self, d: dict, **felder) -> None:
        with self._lock:
            if self._unveraendert(self.db.hole(d["id"]), d):
                self.db.aendern(d["id"], **felder)

    def _blick_anwenden(self, d: dict, art: str, absender: str, datum: date, titel: str, text: str,
                        rest: list[str], erklaerung: str, ki_runde: bool = False, stand: int | None = None) -> bool:
        """Ergebnis eintragen und die Datei passend verschieben. False: inzwischen von Hand bearbeitet o. Ä."""
        titel = titelregeln.gehaltsabrechnung(titel, text, datum, self.heute()) or titel
        felder = {"blick": 1} if ki_runde else {}
        if stand is not None:
            felder["blick_stand"] = stand
        with self._lock:
            if not self._unveraendert(self.db.hole(d["id"]), d):
                return False
            alt = PurePosixPath(d["rel_pfad"])
            neu_rel = ablage.zielpfad(art, absender, datum, titel, alt.suffix.lstrip("."))
            try:
                neu = alt if _ohne_nummer(alt) == neu_rel else ablage.verschieben(alt, neu_rel, self.archiv)
            except OSError:
                log.warning("dok=%d zweiter_blick datei_fehlt", d["id"])
                self.db.aendern(d["id"], **felder)
                return False
            self.db.aendern(d["id"], art=art, absender=absender, datum=datum.isoformat(), titel=titel,
                            rel_pfad=str(neu), unsicher=1 if rest else 0, grund="; ".join(rest),
                            blick_geklaert=0 if rest else 1, blick_text=erklaerung, **felder)
            if not rest:
                self.db.zaehle("wissen")
        return True

    # ── Doppelte von vor 1.8 nachtragen (einmalig) ──────────────────────
    def dubletten_nachtragen(self) -> int:
        """Bis 1.7 wurden doppelte Dateien nur gezählt. Einmal nachrechnen, welche es waren: aufbewahrte Kopien aus
        dem Eingang und alle Import-Ordner (nur lesend). Ist nicht bekannt, welche Datei einer Gruppe damals abgelegt
        wurde, werden alle gezeigt."""
        if self.db.zaehler("dubletten_nachgetragen") >= 1:
            return 0
        log.info("dubletten nachtragen beginnt")
        n = 0
        for p in sorted((self.daten / "dubletten").glob("*")):
            try:
                if not p.is_file() or p.is_symlink() or self.db.dublette_datei_bekannt(str(p)):
                    continue
                sha = _sha256(p)
                if dok := self.db.gibt_sha(sha):
                    n += self.db.dublette_merken("eingang", "(Name nicht gespeichert)",
                                                 config.ANZEIGE_PFADE["eingang"], dok, sha, str(p), nachgetragen=1,
                                                 ts=p.stat().st_mtime)
            except OSError:
                continue
        wartend, gesehen = self.db.wartende_pfade(), set()
        gruppen: dict[int, list] = {}
        for ordner in self.db.import_ordner_alle():
            for p, herkunft in self.quellen.dateien(ordner):
                if self._stop.is_set():
                    return n
                try:
                    echt = os.path.realpath(p)
                    if str(p) in wartend or echt in gesehen:      # überlappende Ordner: jede Datei nur einmal
                        continue
                    gesehen.add(echt)
                    sha = _sha256(p)
                    ts = p.stat().st_mtime
                except OSError:
                    continue
                if dok := self.db.gibt_sha(sha):
                    pfad = self.quellen.anzeige_pfad("/".join(x for x in (ordner, herkunft) if x))
                    gruppen.setdefault(dok, []).append((p.name, pfad, sha, ts))
        for dok, dateien in gruppen.items():
            d = self.db.hole(dok)
            if d and d["quelle"] == "import" and d["quelle_datei"]:
                dateien = [f for f in dateien if f"{f[1]}/{f[0]}" != d["quelle_datei"]]   # das Original selbst
            elif len(dateien) < 2 and d and d["quelle"] == "import":
                continue                      # nur eine Datei mit diesem Inhalt: das ist die abgelegte selbst
            for name, pfad, sha, ts in dateien:
                n += self.db.dublette_merken("import", name, pfad, dok, sha, nachgetragen=1, ts=ts)
        self.db.setze_zaehler("dubletten_nachgetragen", 1)
        log.info("dubletten nachgetragen n=%d", n)
        return n

    # ── Einheitliche Titel (einmalig für den Bestand) ────────────────────
    def titel_vereinheitlichen(self) -> int:
        """Lohnzettel im Bestand auf „Gehaltsabrechnung <Monat> <Jahr>“ umbenennen – einmal je Regel-Fassung."""
        if self.db.zaehler("titel_gehalt") >= 1:
            return 0
        n = 0
        for d in self.db.alle_abgelegt():
            if not titelregeln.ist_gehaltsabrechnung(d["titel"]):
                continue
            neu_titel = titelregeln.gehaltsabrechnung(d["titel"], self.db.text(d["id"]),
                                                      date.fromisoformat(d["datum"]), self.heute())
            if not neu_titel:
                continue
            with self._lock:
                jetzt = self.db.hole(d["id"])
                if not jetzt or jetzt["status"] != "abgelegt" or any(jetzt[k] != d[k] for k in d):
                    continue                  # inzwischen verändert – nicht mit altem Pfad verschieben
                alt = PurePosixPath(d["rel_pfad"])
                neu_rel = ablage.zielpfad(d["art"], d["absender"], date.fromisoformat(d["datum"]), neu_titel,
                                          alt.suffix.lstrip("."))
                try:
                    neu = ablage.verschieben(alt, neu_rel, self.archiv)
                except OSError:
                    continue
                self.db.aendern(d["id"], titel=neu_titel, rel_pfad=str(neu))
                n += 1
        self.db.setze_zaehler("titel_gehalt", 1)
        log.info("titel vereinheitlicht dok=%d", n)
        return n

    # ── Papierkorb ───────────────────────────────────────────────────────
    def in_papierkorb(self, dok_id: int) -> None:
        with self._lock:
            d = self.db.hole(dok_id)
            if not d or d["status"] != "abgelegt":
                return
            q = self.archiv / d["rel_pfad"]
            ziel = self.daten / "papierkorb" / f"{dok_id}{Path(d['rel_pfad']).suffix}"
            shutil.move(str(q), str(ziel))
            ablage.leere_ordner_entfernen(q.parent, self.archiv)
            self.db.aendern(dok_id, status="papierkorb", papierkorb_pfad=str(ziel), papierkorb_ts=self.uhr())

    def wiederherstellen(self, dok_id: int) -> None:
        with self._lock:
            d = self.db.hole(dok_id)
            if not d or d["status"] != "papierkorb":
                return
            neu = ablage.ablegen(Path(d["papierkorb_pfad"]), PurePosixPath(d["rel_pfad"]), self.archiv)
            self.db.aendern(dok_id, status="abgelegt", rel_pfad=str(neu), papierkorb_pfad="", papierkorb_ts=0)

    def _endgueltig(self, d: dict) -> bool:
        """Ein Dokument AUS DEM PAPIERKORB endgültig löschen (nur unter self._lock aufrufen)."""
        if not d or d["status"] != "papierkorb":
            return False
        p = Path(d["papierkorb_pfad"])
        try:
            if p.resolve().parent == (self.daten / "papierkorb").resolve():   # nie etwas außerhalb des Papierkorbs
                p.unlink(missing_ok=True)
        except OSError:
            return False
        vorschau.loeschen(d["id"], self.vorschau_dir)
        self.db.loeschen(d["id"])
        self.db.dubletten_ohne_kopie_loeschen(d["id"], d["sha256"])   # aufbewahrte Kopien bleiben ansehbar
        return True

    def endgueltig_loeschen(self, dok_id: int) -> bool:
        with self._lock:
            return self._endgueltig(self.db.hole(dok_id))

    def papierkorb_leeren(self) -> int:
        with self._lock:
            n = sum(self._endgueltig(d) for d in self.db.papierkorb_alt(float("inf")))
        log.info("papierkorb geleert dok=%d", n)
        return n

    def papierkorb_aufraeumen(self) -> int:
        with self._lock:
            return sum(self._endgueltig(d) for d in self.db.papierkorb_alt(self.uhr() - PAPIERKORB_TAGE * 86400))

    # ── Arten verwalten ──────────────────────────────────────────────────
    def art_umbenennen(self, alt: str, neu: str) -> str:
        """Ordner im Archiv umbenennen und alle Dokumente/Regeln mitziehen."""
        neu = ablage.saeubere(neu, 40)
        with self._lock:
            if neu == alt:
                return neu
            q, z = self.archiv / ablage.saeubere(alt, 40), self.archiv / neu
            if z.exists():
                raise ValueError("Im Archiv gibt es schon einen Ordner mit diesem Namen.")
            if q.exists():
                os.rename(q, z)
            self.db.art_umbenennen(alt, neu)
        log.info("art umbenannt")
        return neu

    # ── Korrektur aus der Oberfläche ─────────────────────────────────────
    def korrigieren(self, dok_id: int, art: str, absender: str, datum: date, titel: str,
                    lernen: bool = True) -> dict:
        with self._lock:
            d = self.db.hole(dok_id)
            if not d or d["status"] != "abgelegt":
                raise KeyError(dok_id)
            alt = PurePosixPath(d["rel_pfad"])
            neu_rel = ablage.zielpfad(art, absender, datum, titel, alt.suffix.lstrip("."))
            # Nur bestätigt (gleicher Platz bis auf „ (2)“) → nichts verschieben.
            neu = alt if _ohne_nummer(alt) == neu_rel else ablage.verschieben(alt, neu_rel, self.archiv)
            geaendert = art != d["art"] or absender.casefold() != d["absender"].casefold()
            if geaendert and lernen:
                self.regeln.lerne(d["absender"], absender, art)
            self.db.aendern(dok_id, art=art, absender=absender, datum=datum.isoformat(), titel=titel,
                            rel_pfad=str(neu), unsicher=0, grund="", blick_geklaert=0, blick_text="")
            self.db.zaehle("wissen")
            log.info("dok=%d korrigiert regel=%d", dok_id, int(geaendert))
            return self.db.hole(dok_id)
