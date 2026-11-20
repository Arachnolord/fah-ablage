# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Vorschaubild der ersten Seite (wie die Kacheln in Paperless).

PDFs: Bild der ersten Seite per pdftoppm (DATA_DIR/vorschau/<id>.png).
Alles andere (CSV, TXT, Word, Excel …): ein „Blatt Papier“ mit den ersten Zeilen
des erkannten Texts als SVG (DATA_DIR/vorschau/<id>.svg) – sonst sähe eine
Kachel leer aus, obwohl die Datei Inhalt hat.
"""
import csv
import io
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

BREITE = 360
ZEILEN = 22          # Textzeilen auf dem Kachel-Blatt
SPALTEN = 40         # Zeichen je Zeile
TEXT_MAX = 100_000   # so viel Text zeigt die Dokument-Ansicht höchstens
CSV_ZEILEN = 500     # so viele CSV-Zeilen zeigt die Dokument-Ansicht höchstens
_lock = threading.Lock()
PLATZHALTER = "Kein Text gefunden"
# Nur im Platzhalter-SVG möglich: Text aus Dokumenten wird escaped, kann also kein „\">…</text>“ enthalten
_PLATZHALTER_MARKE = f'#66706a">{PLATZHALTER}</text>'.encode()
# Zeichen, die in XML verboten sind (Steuerzeichen, Ersatz-Hälften) – würden das SVG zerbrechen
_XML_VERBOTEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _lauf(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(args, 1, b"", b"")


def _blatt_zeilen(text: str) -> list[str]:
    zeilen: list[str] = []
    for roh in (text or "").splitlines():
        z = _XML_VERBOTEN.sub("", roh).replace("\t", "  ").rstrip()
        if not z.strip():
            if zeilen and zeilen[-1]:
                zeilen.append("")          # Leerzeilen zusammenfassen
            continue
        zeilen.append(z[:SPALTEN] + ("…" if len(z) > SPALTEN else ""))
        if len(zeilen) >= ZEILEN:
            break
    while zeilen and not zeilen[-1]:
        zeilen.pop()
    return zeilen


def blatt_svg(text: str, endung: str) -> str:
    """SVG-Blatt mit den ersten Textzeilen; ohne Text ein klarer Hinweis statt leerer Fläche."""
    kopf = ('<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="360" height="480" '
            'viewBox="0 0 300 400"><rect width="300" height="400" fill="#ffffff"/>')
    zeilen = _blatt_zeilen(text)
    if not zeilen:
        marke = escape(_XML_VERBOTEN.sub("", (endung or "Datei").upper())[:8])
        return (kopf + '<g font-family="system-ui, -apple-system, Segoe UI, sans-serif" text-anchor="middle">'
                f'<text x="150" y="190" font-size="34" font-weight="700" fill="#9aa49e">{marke}</text>'
                f'<text x="150" y="222" font-size="14" fill="#66706a">{PLATZHALTER}</text></g></svg>')
    teile = [kopf, '<g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
                   'font-size="11" fill="#2b302d">']
    for i, z in enumerate(zeilen):
        teile.append(f'<text x="16" y="{26 + i * 16}">{escape(z)}</text>')
    teile.append("</g></svg>")
    return "".join(teile)


def _schreiben(ziel: Path, inhalt: str) -> None:
    tmp = ziel.with_name(f".{ziel.name}.{os.getpid()}.tmp")
    tmp.write_text(inhalt, encoding="utf-8", errors="replace")
    os.replace(tmp, ziel)


def bild(dok_id: int, datei: Path, ordner: Path,
         text_holen: Callable[[], str] | None = None) -> Path | None:
    ordner = Path(ordner)
    png = ordner / f"{int(dok_id)}.png"
    svg = ordner / f"{int(dok_id)}.svg"
    if png.exists():
        return png
    text = None
    if svg.exists():
        if _PLATZHALTER_MARKE not in svg.read_bytes():
            return svg
        text = text_holen() if text_holen else ""   # vielleicht inzwischen lesbar (neuer Dateityp)
        if not text.strip():
            return svg
        svg.unlink(missing_ok=True)
    if not Path(datei).is_file():
        return None
    endung = Path(datei).suffix.lower().lstrip(".")
    if endung != "pdf" and text is None:
        text = text_holen() if text_holen else ""   # außerhalb der Sperre: langsame Dateien bremsen keine PDFs
    with _lock:   # pdftoppm nie mehrfach gleichzeitig (NAS schonen)
        if png.exists():
            return png
        ordner.mkdir(parents=True, exist_ok=True)
        if endung == "pdf":
            r = _lauf(["pdftoppm", "-png", "-f", "1", "-l", "1", "-singlefile",
                       "-scale-to-x", str(BREITE), "-scale-to-y", "-1", str(datei), str(png.with_suffix(""))])
            if r.returncode != 0 or not png.exists():
                png.unlink(missing_ok=True)
                return None       # nicht merken – beim nächsten Abruf neu versuchen
            return png
        _schreiben(svg, blatt_svg(text or "", endung))
    return svg


def ist_platzhalter(pfad: Path) -> bool:
    return Path(pfad).suffix == ".svg" and _PLATZHALTER_MARKE in Path(pfad).read_bytes()


def loeschen(dok_id: int, ordner: Path) -> None:
    for endung in ("png", "svg", "gelesen"):
        (Path(ordner) / f"{int(dok_id)}.{endung}").unlink(missing_ok=True)


def tabelle_aus_csv(text: str) -> tuple[list[list[str]], bool] | None:
    """CSV als Zeilenliste (höchstens CSV_ZEILEN) + ob gekürzt; None, wenn es keine echte Tabelle ist."""
    if not text or not text.strip():
        return None
    try:
        dialekt = csv.Sniffer().sniff(text[:20_000], delimiters=";,\t|")
        zeilen: list[list[str]] = []
        for z in csv.reader(io.StringIO(text), dialekt):
            if len(zeilen) >= CSV_ZEILEN:
                return zeilen, True
            zeilen.append(z)
    except csv.Error:
        return None
    if not zeilen or max(len(z) for z in zeilen) < 2:
        return None
    return zeilen, False


def ansicht(endung: str, text: str) -> dict:
    """Was die Dokument-Ansicht für Nicht-PDFs zeigt: CSV als Tabelle, sonst der erkannte Text."""
    gekuerzt = len(text or "") > TEXT_MAX
    text = (text or "")[:TEXT_MAX]
    if endung in ("csv", "tsv"):
        t = tabelle_aus_csv(text)
        if t:
            return {"vorschau_tabelle": t[0], "vorschau_gekuerzt": t[1] or gekuerzt}
    if endung in ("xls", "xlsx", "xlsm") and "\t" in text:
        zeilen = [z.split("\t") for z in text.splitlines()[:CSV_ZEILEN + 1]]
        return {"vorschau_tabelle": zeilen[:CSV_ZEILEN],
                "vorschau_gekuerzt": len(zeilen) > CSV_ZEILEN or gekuerzt}
    if text.strip():
        return {"vorschau_text": text, "vorschau_gekuerzt": gekuerzt}
    return {}
