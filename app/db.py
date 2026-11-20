# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""SQLite: Dokumente, Volltext (FTS5), Warteschlange, gelernte Regeln."""
import re
import sqlite3
from collections import Counter
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dokumente(
    id INTEGER PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    rel_pfad TEXT NOT NULL,
    art TEXT NOT NULL,
    absender TEXT NOT NULL,
    datum TEXT NOT NULL,
    titel TEXT NOT NULL,
    unsicher INTEGER NOT NULL DEFAULT 0,
    grund TEXT NOT NULL DEFAULT '',
    quelle TEXT NOT NULL DEFAULT 'eingang',
    herkunft TEXT NOT NULL DEFAULT '',
    eingang_ts REAL NOT NULL DEFAULT 0,
    sicherheit REAL NOT NULL DEFAULT 0,
    ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'abgelegt',
    papierkorb_pfad TEXT NOT NULL DEFAULT '',
    papierkorb_ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_dok_datum ON dokumente(datum);
CREATE INDEX IF NOT EXISTS ix_dok_art ON dokumente(art);
CREATE VIRTUAL TABLE IF NOT EXISTS volltext USING fts5(
    text, titel, absender, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS warteschlange(
    id INTEGER PRIMARY KEY,
    pfad TEXT UNIQUE NOT NULL,
    quelle TEXT NOT NULL,
    herkunft TEXT NOT NULL DEFAULT '',
    versuche INTEGER NOT NULL DEFAULT 0,
    naechster_ts REAL NOT NULL DEFAULT 0,
    eingereiht_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS aliase(roh TEXT PRIMARY KEY, kanon TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS absender_art(absender TEXT PRIMARY KEY, art TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zaehler(name TEXT PRIMARY KEY, wert INTEGER NOT NULL DEFAULT 0);
-- Byte-gleiche Dateien, die NICHT noch einmal abgelegt wurden (seit 1.8). datei: aufbewahrte Kopie (nur Eingang/Upload).
CREATE TABLE IF NOT EXISTS dubletten(
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    quelle TEXT NOT NULL,
    name TEXT NOT NULL,
    pfad TEXT NOT NULL DEFAULT '',
    dok_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    datei TEXT NOT NULL DEFAULT '',
    nachgetragen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pfad, name, sha256, datei)
);
CREATE INDEX IF NOT EXISTS ix_dub_dok ON dubletten(dok_id);
"""

_FELDER = ("sha256", "rel_pfad", "art", "absender", "datum", "titel", "unsicher", "grund",
           "quelle", "herkunft", "eingang_ts", "sicherheit", "ms", "status",
           "papierkorb_pfad", "papierkorb_ts", "ordner", "blick", "blick_stand", "blick_geklaert", "blick_text",
           "erst_grund", "quelle_datei")
# Spalten, die nach 1.0 dazukamen: (Tabelle, Spalte, Definition) – werden beim Start nachgerüstet.
_NACHRUESTEN = (("dokumente", "ordner", "TEXT NOT NULL DEFAULT ''"),
                ("warteschlange", "ordner", "TEXT NOT NULL DEFAULT ''"),
                # Zweiter Blick (1.7): KI-Runde erledigt, Wissensstand der letzten Nachbarsuche, geklärt + Erklärung
                ("dokumente", "blick", "INTEGER NOT NULL DEFAULT 0"),
                ("dokumente", "blick_stand", "INTEGER NOT NULL DEFAULT 0"),
                ("dokumente", "blick_geklaert", "INTEGER NOT NULL DEFAULT 0"),
                ("dokumente", "blick_text", "TEXT NOT NULL DEFAULT ''"),
                ("dokumente", "erst_grund", "TEXT NOT NULL DEFAULT ''"),
                ("dokumente", "quelle_datei", "TEXT NOT NULL DEFAULT ''"))   # woher die abgelegte Datei kam (1.8)


def _fts_anfrage(text: str) -> str:
    woerter = re.findall(r"\w+", text or "", flags=re.UNICODE)
    return " AND ".join(f'"{w}"*' for w in woerter[:12])


_SPALTEN = {"datum": "d.datum", "titel": "d.titel COLLATE NOCASE", "absender": "d.absender COLLATE NOCASE",
            "art": "d.art COLLATE NOCASE", "eingang": "d.eingang_ts"}


def _sortierung(name: str, absteigend: bool) -> str:
    spalte = _SPALTEN.get(name, _SPALTEN["datum"])
    richtung = "DESC" if absteigend else "ASC"
    return f"{spalte} {richtung}, d.id {richtung}"


class Db:
    def __init__(self, pfad: Path):
        Path(pfad).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(str(pfad), check_same_thread=False, timeout=30)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.execute("PRAGMA journal_mode=WAL")
            self._c.executescript(_SCHEMA)
            for tab, spalte, art in _NACHRUESTEN:
                if spalte not in {r[1] for r in self._c.execute(f"PRAGMA table_info({tab})")}:
                    self._c.execute(f"ALTER TABLE {tab} ADD COLUMN {spalte} {art}")
                    if (tab, spalte) == ("dokumente", "ordner"):   # einmalig: Importe von vor 1.6 zuordnen
                        self._c.execute("INSERT OR REPLACE INTO zaehler(name,wert) VALUES('ordner_nachtragen',1)")
            self._c.commit()

    def _x(self, sql, args=()):
        with self._lock:
            cur = self._c.execute(sql, args)
            self._c.commit()
            return cur

    def _alle(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args).fetchall()]

    # ── Dokumente ────────────────────────────────────────────────────────
    def einfuegen(self, text: str = "", **felder) -> int:
        f = {k: v for k, v in felder.items() if k in _FELDER}
        f.setdefault("eingang_ts", time.time())
        with self._lock:
            cur = self._c.execute(
                f"INSERT INTO dokumente({','.join(f)}) VALUES({','.join('?' * len(f))})",
                tuple(f.values()))
            i = cur.lastrowid
            self._c.execute("INSERT INTO volltext(rowid,text,titel,absender) VALUES(?,?,?,?)",
                            (i, text or "", f.get("titel", ""), f.get("absender", "")))
            self._c.commit()
            return i

    def aendern(self, id: int, text: str | None = None, **felder) -> None:
        f = {k: v for k, v in felder.items() if k in _FELDER}
        with self._lock:
            if f:
                self._c.execute(f"UPDATE dokumente SET {','.join(k + '=?' for k in f)} WHERE id=?",
                                (*f.values(), id))
            if text is not None or "titel" in f or "absender" in f:
                alt = self._c.execute("SELECT text,titel,absender FROM volltext WHERE rowid=?",
                                      (id,)).fetchone()
                d = self._c.execute("SELECT titel,absender FROM dokumente WHERE id=?", (id,)).fetchone()
                neu_text = text if text is not None else (alt["text"] if alt else "")
                self._c.execute("DELETE FROM volltext WHERE rowid=?", (id,))
                self._c.execute("INSERT INTO volltext(rowid,text,titel,absender) VALUES(?,?,?,?)",
                                (id, neu_text, d["titel"], d["absender"]))
            self._c.commit()

    def text_nachtragen(self, id: int, sha256: str, text: str) -> bool:
        """Text für die Suche nachtragen – nur, wenn die ID noch dasselbe Dokument ist."""
        with self._lock:
            d = self._c.execute("SELECT titel,absender FROM dokumente WHERE id=? AND sha256=?",
                                (id, sha256)).fetchone()
            if not d:
                return False
            self._c.execute("DELETE FROM volltext WHERE rowid=?", (id,))
            self._c.execute("INSERT INTO volltext(rowid,text,titel,absender) VALUES(?,?,?,?)",
                            (id, text, d["titel"], d["absender"]))
            self._c.commit()
            return True

    def hole(self, id: int) -> dict | None:
        r = self._alle("SELECT * FROM dokumente WHERE id=?", (id,))
        return r[0] if r else None

    def text(self, id: int) -> str:
        r = self._alle("SELECT text FROM volltext WHERE rowid=?", (id,))
        return r[0]["text"] if r else ""

    def gibt_sha(self, sha: str) -> int | None:
        r = self._alle("SELECT id FROM dokumente WHERE sha256=?", (sha,))
        return r[0]["id"] if r else None

    def suche(self, art=None, absender=None, von=None, bis=None, text=None,
              nur_unsicher=False, limit=50, offset=0, status="abgelegt", nur_blick=False,
              sortierung="datum", absteigend=True):
        wo, args = ["d.status=?"], [status]
        if art:
            wo.append("d.art=?"); args.append(art)
        if absender:
            wo.append("d.absender=?"); args.append(absender)
        if von:
            wo.append("d.datum>=?"); args.append(von)
        if bis:
            wo.append("d.datum<=?"); args.append(bis)
        if nur_unsicher:
            wo.append("d.unsicher=1")
        if nur_blick:
            wo.append("d.blick_geklaert=1 AND d.unsicher=0")
        q = _fts_anfrage(text) if text else ""
        if text and not q:
            return [], 0
        if q:
            wo.append("d.id IN (SELECT rowid FROM volltext WHERE volltext MATCH ?)"); args.append(q)
        bed = " AND ".join(wo)
        n = self._alle(f"SELECT COUNT(*) n FROM dokumente d WHERE {bed}", args)[0]["n"]
        treffer = self._alle(
            f"SELECT d.* FROM dokumente d WHERE {bed} ORDER BY {_sortierung(sortierung, absteigend)} LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)])
        return treffer, n

    def absender_liste(self, art=None) -> list[str]:
        if art:
            r = self._alle("SELECT DISTINCT absender FROM dokumente WHERE status='abgelegt' AND art=?"
                           " ORDER BY absender COLLATE NOCASE", (art,))
        else:
            r = self._alle("SELECT DISTINCT absender FROM dokumente WHERE status='abgelegt'"
                           " ORDER BY absender COLLATE NOCASE")
        return [x["absender"] for x in r]

    def art_verteilung(self, absender: str):
        from collections import Counter
        r = self._alle("SELECT art, COUNT(*) n FROM dokumente WHERE status='abgelegt' AND absender=? "
                       "COLLATE NOCASE GROUP BY art", (absender,))
        return Counter({x["art"]: x["n"] for x in r})

    def naechster_unsicher(self, datum: str | None, nach_id: int | None, ausser: int | None = None) -> int | None:
        """Nächstes unsicheres Dokument in Listenreihenfolge (Datum absteigend) nach dem aktuellen; sonst von vorn."""
        ausser = -1 if ausser is None else ausser
        if datum is not None and nach_id is not None:
            r = self._alle("SELECT id FROM dokumente WHERE status='abgelegt' AND unsicher=1 AND id!=? "
                           "AND (datum<? OR (datum=? AND id<?)) ORDER BY datum DESC, id DESC LIMIT 1",
                           (ausser, datum, datum, nach_id))
            if r:
                return r[0]["id"]
        r = self._alle("SELECT id FROM dokumente WHERE status='abgelegt' AND unsicher=1 AND id!=? "
                       "ORDER BY datum DESC, id DESC LIMIT 1", (ausser,))
        return r[0]["id"] if r else None

    # ── Sicherung ────────────────────────────────────────────────────────
    def sicherungskopie(self, ziel: Path) -> None:
        """Konsistente Kopie der Datenbank (SQLite-Backup-API, auch während geschrieben wird)."""
        Path(ziel).unlink(missing_ok=True)
        with self._lock:
            dst = sqlite3.connect(str(ziel))
            try:
                self._c.backup(dst)
            finally:
                dst.close()

    def papierkorb_alle(self) -> list[dict]:
        return self._alle("SELECT * FROM dokumente WHERE status='papierkorb'")

    def anzahl_dokumente_alle(self) -> int:
        return self._alle("SELECT COUNT(*) n FROM dokumente")[0]["n"]

    def ist_leer(self) -> bool:
        return self.anzahl_dokumente_alle() == 0 and self.anzahl() == 0

    def uebernehmen(self, quelle: Path, papierkorb: Path) -> int:
        """Inhalt einer gesicherten Datenbank in diese (leere) übernehmen – in EINER Transaktion."""
        with self._lock:
            self._c.execute("ATTACH DATABASE ? AS b", (str(quelle),))
            try:
                eigene = [r[1] for r in self._c.execute("PRAGMA main.table_info(dokumente)")]
                fremde = {r[1] for r in self._c.execute("PRAGMA b.table_info(dokumente)")}
                spalten = ",".join(s for s in eigene if s in fremde)
                with self._c:
                    if "ordner" not in fremde:      # Sicherung von vor 1.6: Importe später zuordnen
                        self._c.execute("INSERT OR REPLACE INTO main.zaehler(name,wert) "
                                        "VALUES('ordner_nachtragen',1)")
                    if "erst_grund" not in fremde:  # Sicherung von vor 1.7: Lohnzettel-Titel noch vereinheitlichen
                        self._c.execute("INSERT OR REPLACE INTO main.zaehler(name,wert) VALUES('titel_gehalt',0)")
                    self._c.execute(f"INSERT INTO main.dokumente({spalten}) SELECT {spalten} FROM b.dokumente")
                    self._c.execute("INSERT INTO main.volltext(rowid,text,titel,absender) "
                                    "SELECT rowid,text,titel,absender FROM b.volltext")
                    for tab in ("aliase", "absender_art"):
                        if self._c.execute("SELECT 1 FROM b.sqlite_master WHERE name=?", (tab,)).fetchone():
                            self._c.execute(f"INSERT OR REPLACE INTO main.{tab} SELECT * FROM b.{tab}")
                    self._c.execute("UPDATE main.dokumente SET blick_stand=0")   # Zähler „wissen“ beginnt neu
                    for i, alt in self._c.execute("SELECT id, papierkorb_pfad FROM main.dokumente "
                                                  "WHERE papierkorb_pfad!=''").fetchall():
                        self._c.execute("UPDATE main.dokumente SET papierkorb_pfad=? WHERE id=?",
                                        (str(Path(papierkorb) / Path(alt).name), i))
                n = self._c.execute("SELECT COUNT(*) FROM main.dokumente").fetchone()[0]
            finally:
                self._c.execute("DETACH DATABASE b")
            return n

    def art_umbenennen(self, alt: str, neu: str) -> None:
        with self._lock, self._c:
            praefix = alt + "/"
            self._c.execute("UPDATE dokumente SET art=?, rel_pfad=? || substr(rel_pfad, ?) "
                            "WHERE art=? AND substr(rel_pfad, 1, ?)=?",
                            (neu, neu + "/", len(praefix) + 1, alt, len(praefix), praefix))
            self._c.execute("UPDATE dokumente SET art=? WHERE art=?", (neu, alt))
            self._c.execute("UPDATE absender_art SET art=? WHERE art=?", (neu, alt))

    def anzahl_art(self, art: str) -> int:
        return self._alle("SELECT COUNT(*) n FROM dokumente WHERE art=?", (art,))[0]["n"]

    def arten_zaehlung(self) -> dict:
        r = self._alle("SELECT art, COUNT(*) n FROM dokumente WHERE status='abgelegt' GROUP BY art ORDER BY n DESC")
        return {x["art"]: x["n"] for x in r}

    def absender_zaehlung(self) -> list[dict]:
        """Je Absender: Anzahl, häufigste Art, jüngstes Briefdatum."""
        r = self._alle("SELECT absender, art, COUNT(*) n, MAX(datum) zuletzt FROM dokumente "
                       "WHERE status='abgelegt' GROUP BY absender, art")
        aus: dict[str, dict] = {}
        for x in r:
            e = aus.setdefault(x["absender"], {"absender": x["absender"], "anzahl": 0, "art": x["art"],
                                               "art_n": 0, "zuletzt": x["zuletzt"]})
            e["anzahl"] += x["n"]
            e["zuletzt"] = max(e["zuletzt"], x["zuletzt"])
            if x["n"] > e["art_n"]:
                e["art"], e["art_n"] = x["art"], x["n"]
        return sorted(aus.values(), key=lambda e: e["absender"].casefold())

    def zahlen(self) -> dict:
        z = self._alle("SELECT COUNT(*) n, COALESCE(SUM(unsicher),0) u FROM dokumente WHERE status='abgelegt'")[0]
        w = self._alle("SELECT COUNT(*) n FROM warteschlange")[0]["n"]
        return {"abgelegt": z["n"], "unsicher": z["u"], "warteschlange": w,
                "dubletten": self.zaehler("dubletten")}

    def papierkorb_alt(self, vor_ts: float) -> list[dict]:
        return self._alle("SELECT * FROM dokumente WHERE status='papierkorb' AND papierkorb_ts<?", (vor_ts,))

    def loeschen(self, id: int) -> None:
        with self._lock:
            self._c.execute("DELETE FROM dokumente WHERE id=?", (id,))
            self._c.execute("DELETE FROM volltext WHERE rowid=?", (id,))
            self._c.commit()

    def bericht(self, ordner: str | None = None) -> list[dict]:
        """Je oberstem Import-Ursprungsordner: Arten-Verteilung, unsicher, Einheitlichkeit."""
        sql, args = "SELECT herkunft, art, unsicher FROM dokumente WHERE status='abgelegt' AND quelle='import'", ()
        if ordner is not None:
            sql, args = sql + " AND ordner=?", (ordner,)
        zeilen = self._alle(sql, args)
        gruppen: dict[str, dict] = {}
        for z in zeilen:
            top = (z["herkunft"] or "(oberste Ebene)").split("/")[0]
            g = gruppen.setdefault(top, {"herkunft": top, "gesamt": 0, "unsicher": 0, "arten": {}})
            g["gesamt"] += 1
            g["unsicher"] += z["unsicher"]
            g["arten"][z["art"]] = g["arten"].get(z["art"], 0) + 1
        for g in gruppen.values():
            g["einheitlich"] = max(g["arten"].values()) / g["gesamt"]
        return sorted(gruppen.values(), key=lambda g: (-g["gesamt"], g["herkunft"]))

    # ── Zähler ───────────────────────────────────────────────────────────
    def zaehler(self, name: str) -> int:
        r = self._alle("SELECT wert FROM zaehler WHERE name=?", (name,))
        return r[0]["wert"] if r else 0

    def zaehle(self, name: str, um: int = 1) -> None:
        self._x("INSERT INTO zaehler(name,wert) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET wert=wert+excluded.wert", (name, um))

    def setze_zaehler(self, name: str, wert: int) -> None:
        self._x("INSERT INTO zaehler(name,wert) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET wert=excluded.wert", (name, wert))

    # ── Warteschlange ────────────────────────────────────────────────────
    def einreihen(self, pfad: str, quelle: str, herkunft: str = "", ordner: str = "") -> bool:
        try:
            self._x("INSERT INTO warteschlange(pfad,quelle,herkunft,ordner,eingereiht_ts) VALUES(?,?,?,?,?)",
                    (str(pfad), quelle, herkunft, ordner, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    def naechster(self, jetzt: float | None = None) -> dict | None:
        jetzt = time.time() if jetzt is None else jetzt
        r = self._alle("SELECT * FROM warteschlange WHERE naechster_ts<=? "
                       "ORDER BY (quelle='import'), id LIMIT 1", (jetzt,))
        return r[0] if r else None

    def hole_auftrag(self, id: int) -> dict | None:
        r = self._alle("SELECT * FROM warteschlange WHERE id=?", (id,))
        return r[0] if r else None

    def fehlschlag(self, id: int, naechster_ts: float) -> None:
        self._x("UPDATE warteschlange SET versuche=versuche+1, naechster_ts=? WHERE id=?",
                (naechster_ts, id))

    def entfernen(self, id: int) -> bool:
        """True nur, wenn der Auftrag noch da war (sonst z. B. inzwischen per „Import abbrechen“ entfernt)."""
        return self._x("DELETE FROM warteschlange WHERE id=?", (id,)).rowcount > 0

    def setze_auftrag(self, id: int, versuche: int, naechster_ts: float) -> None:
        self._x("UPDATE warteschlange SET versuche=?, naechster_ts=? WHERE id=?", (versuche, naechster_ts, id))

    def warteschlange_leeren(self, quelle: str, ordner: str | None = None) -> int:
        if ordner is None:
            return self._x("DELETE FROM warteschlange WHERE quelle=?", (quelle,)).rowcount
        return self._x("DELETE FROM warteschlange WHERE quelle=? AND ordner=?", (quelle, ordner)).rowcount

    def alle_aus_quelle(self, quelle: str, ordner: str | None = None) -> list[dict]:
        if ordner is None:
            return self._alle("SELECT * FROM dokumente WHERE quelle=?", (quelle,))
        return self._alle("SELECT * FROM dokumente WHERE quelle=? AND ordner=?", (quelle, ordner))

    def anzahl_import(self, ordner: str) -> int:
        """Abgelegte Kopien (auch im Papierkorb) aus diesem Import-Ordner."""
        return self._alle("SELECT COUNT(*) n FROM dokumente WHERE quelle='import' AND ordner=?", (ordner,))[0]["n"]

    def ordner_nachtragen(self, ordner: str, wurzel: str = "/quellen") -> int:
        """EINMALIG nach dem Umstieg auf 1.6: Importe aus dem festen Altbestand-Ordner bekommen ihren Ordner,
        offene Aufträge den neuen Pfad (/import/… → <wurzel>/<ordner>/…). Danach heißt ordner='' „ganz oben“."""
        with self._lock, self._c:
            r = self._c.execute("SELECT wert FROM zaehler WHERE name='ordner_nachtragen'").fetchone()
            if not r or r[0] != 1:
                return 0
            n = self._c.execute("UPDATE dokumente SET ordner=? WHERE quelle='import' AND ordner=''", (ordner,)).rowcount
            neu = "/".join(x for x in (wurzel.rstrip("/"), ordner) if x) + "/"
            self._c.execute("UPDATE warteschlange SET ordner=?, pfad=? || substr(pfad, 9) "
                            "WHERE quelle='import' AND ordner='' AND substr(pfad, 1, 8)='/import/'", (ordner, neu))
            self._c.execute("UPDATE zaehler SET wert=0 WHERE name='ordner_nachtragen'")
            return n

    # ── Zweiter Blick ────────────────────────────────────────────────────
    def nachbar_kandidaten(self, dok_id: int, worte: list[str], limit: int = 40) -> list[dict]:
        """Sichere Dokumente, die viele dieser Wörter enthalten (Vorauswahl per Volltext, Text dabei)."""
        if not worte:
            return []
        anfrage = " OR ".join(f'"{w}"' for w in worte)
        return self._alle("SELECT d.id, d.art, d.absender, d.titel, v.text FROM volltext v "
                          "JOIN dokumente d ON d.id=v.rowid WHERE volltext MATCH ? AND d.id!=? "
                          "AND d.unsicher=0 AND d.blick_geklaert=0 AND d.status='abgelegt' "
                          "ORDER BY bm25(volltext) LIMIT ?",
                          (anfrage, dok_id, int(limit)))

    def art_verteilung_geprueft(self, absender: str):
        """Wie frühere Briefe dieses Absenders eingeordnet sind – nur sichere, nicht maschinell geklärte."""
        r = self._alle("SELECT art, COUNT(*) n FROM dokumente WHERE status='abgelegt' AND unsicher=0 "
                       "AND blick_geklaert=0 AND absender=? COLLATE NOCASE GROUP BY art", (absender,))
        return Counter({x["art"]: x["n"] for x in r})

    def fuer_blick(self, stand: int) -> list[int]:
        """Unsichere Dokumente, die seit der letzten Nachbarsuche neues Wissen sehen könnten."""
        return [r["id"] for r in self._alle("SELECT id FROM dokumente WHERE status='abgelegt' AND unsicher=1 "
                                            "AND blick_stand<? ORDER BY id", (stand,))]

    def naechster_blick_ki(self) -> int | None:
        r = self._alle("SELECT id FROM dokumente WHERE status='abgelegt' AND unsicher=1 AND blick=0 "
                       "ORDER BY id LIMIT 1")
        return r[0]["id"] if r else None

    def blick_zuruecksetzen(self) -> int:
        return self._x("UPDATE dokumente SET blick=0, blick_stand=0 WHERE status='abgelegt' AND unsicher=1").rowcount

    def alle_abgelegt(self) -> list[dict]:
        return self._alle("SELECT id, art, absender, datum, titel, rel_pfad FROM dokumente WHERE status='abgelegt'")

    def blick_zahlen(self) -> dict:
        r = self._alle("SELECT COALESCE(SUM(unsicher=1 AND blick=0),0) offen, "
                       "COALESCE(SUM(unsicher=0 AND blick_geklaert=1),0) geklaert "
                       "FROM dokumente WHERE status='abgelegt'")[0]
        return {"offen": r["offen"], "geklaert": r["geklaert"]}

    # ── Doppelte (byte-gleiche) Dateien ──────────────────────────────────
    def dublette_merken(self, quelle: str, name: str, pfad: str, dok_id: int, sha256: str, datei: str = "",
                        nachgetragen: int = 0, ts: float | None = None) -> bool:
        cur = self._x("INSERT OR IGNORE INTO dubletten(ts,quelle,name,pfad,dok_id,sha256,datei,nachgetragen) "
                      "VALUES(?,?,?,?,?,?,?,?)", (time.time() if ts is None else ts, quelle, name, pfad, dok_id,
                                                  sha256, datei, nachgetragen))
        return cur.rowcount > 0

    def dubletten_gruppen(self, limit: int = 2000) -> list[dict]:
        """Je abgelegtem Dokument die byte-gleichen Dateien, neueste Gruppe zuerst."""
        # Über ID UND Fingerabdruck verknüpft: IDs werden nach dem Löschen neu vergeben.
        zeilen = self._alle("SELECT u.*, d.titel, d.datum, d.art, d.absender, d.status, d.quelle_datei, "
                            "d.quelle AS dok_quelle FROM dubletten u "
                            "LEFT JOIN dokumente d ON d.id=u.dok_id AND d.sha256=u.sha256 "
                            "ORDER BY u.ts DESC, u.id DESC LIMIT ?", (int(limit),))
        gruppen: dict[int, dict] = {}
        for z in zeilen:
            g = gruppen.setdefault(z["dok_id"], {"dok_id": z["dok_id"], "titel": z["titel"], "datum": z["datum"],
                                                  "art": z["art"], "absender": z["absender"], "status": z["status"],
                                                  "quelle_datei": z["quelle_datei"], "dok_quelle": z["dok_quelle"],
                                                  "dateien": []})
            g["dateien"].append(z)
        return list(gruppen.values())

    def wartende_pfade(self) -> set[str]:
        return {r["pfad"] for r in self._alle("SELECT pfad FROM warteschlange")}

    def import_ordner_alle(self) -> list[str]:
        r = self._alle("SELECT DISTINCT ordner FROM dokumente WHERE quelle='import' "
                       "UNION SELECT DISTINCT ordner FROM warteschlange WHERE quelle='import'")
        return sorted(x["ordner"] for x in r)

    def warteschlange_liste(self, limit: int = 300) -> list[dict]:
        """In der Reihenfolge, in der der Arbeiter sie nimmt (Eingang/Upload vor Import)."""
        return self._alle("SELECT * FROM warteschlange ORDER BY (quelle='import'), id LIMIT ?", (int(limit),))

    def warteschlange_zaehlung(self) -> dict:
        r = self._alle("SELECT quelle, ordner, COUNT(*) n FROM warteschlange GROUP BY quelle, ordner")
        return {"gesamt": sum(x["n"] for x in r), "gruppen": r}

    def dublette(self, id: int) -> dict | None:
        r = self._alle("SELECT * FROM dubletten WHERE id=?", (id,))
        return r[0] if r else None

    def dublette_loeschen(self, id: int) -> None:
        self._x("DELETE FROM dubletten WHERE id=?", (id,))

    def dubletten_zu(self, dok_id: int) -> int:
        return self._alle("SELECT COUNT(*) n FROM dubletten u JOIN dokumente d ON d.id=u.dok_id "
                          "AND d.sha256=u.sha256 WHERE u.dok_id=?", (dok_id,))[0]["n"]

    def dubletten_ohne_kopie_loeschen(self, dok_id: int, sha256: str) -> None:
        self._x("DELETE FROM dubletten WHERE dok_id=? AND sha256=? AND datei=''", (dok_id, sha256))

    def dublette_datei_bekannt(self, datei: str) -> bool:
        return bool(self._alle("SELECT 1 FROM dubletten WHERE datei=?", (datei,)))

    def einreihen_viele(self, eintraege: list[tuple[str, str, str, str]]) -> int:
        """(pfad, quelle, herkunft, ordner) in EINER Transaktion; schon wartende Pfade bleiben. Zahl der neuen."""
        jetzt = time.time()
        with self._lock, self._c:
            vorher = self._c.total_changes
            self._c.executemany("INSERT OR IGNORE INTO warteschlange(pfad,quelle,herkunft,ordner,eingereiht_ts) "
                                "VALUES(?,?,?,?,?)", [(*e, jetzt) for e in eintraege])
            return self._c.total_changes - vorher

    def anzahl(self, quelle: str | None = None) -> int:
        if quelle:
            return self._alle("SELECT COUNT(*) n FROM warteschlange WHERE quelle=?", (quelle,))[0]["n"]
        return self._alle("SELECT COUNT(*) n FROM warteschlange")[0]["n"]

    def in_warteschlange(self, pfad: str) -> bool:
        return bool(self._alle("SELECT 1 FROM warteschlange WHERE pfad=?", (str(pfad),)))

    # ── Regeln ───────────────────────────────────────────────────────────
    def alias(self, roh: str) -> str | None:
        r = self._alle("SELECT kanon FROM aliase WHERE roh=?", (roh,))
        return r[0]["kanon"] if r else None

    def setze_alias(self, roh: str, kanon: str) -> None:
        self._x("INSERT INTO aliase(roh,kanon) VALUES(?,?) "
                "ON CONFLICT(roh) DO UPDATE SET kanon=excluded.kanon", (roh, kanon))

    def regel_art(self, absender: str) -> str | None:
        r = self._alle("SELECT art FROM absender_art WHERE absender=?", (absender,))
        return r[0]["art"] if r else None

    def setze_regel_art(self, absender: str, art: str) -> None:
        self._x("INSERT INTO absender_art(absender,art) VALUES(?,?) "
                "ON CONFLICT(absender) DO UPDATE SET art=excluded.art", (absender, art))

    def regeln_liste(self) -> list[dict]:
        return self._alle("SELECT absender, art FROM absender_art ORDER BY absender")
