# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Text aus Dokumenten holen; Scans und Fotos per Texterkennung (Deutsch) lesbar machen.

Abgelegt wird bei Scans die durchsuchbare Fassung, sonst das Original.
Fehlermeldungen enthalten nie Inhalt oder Dateinamen.
"""
import datetime as dt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MIN_ZEICHEN_JE_SEITE = 80
MAX_TEXT = 300_000
MAX_ZELLEN = 500_000   # Tabellen: so viele Zellen höchstens ansehen (bösartige Riesenblätter)
LESER_VERSION = 2      # erhöhen, wenn neue Formate dazukommen → alte Leseversuche werden wiederholt
SPRACHEN = "deu"  # nur Deutsch: deutlich schneller, lateinische Schrift liest es trotzdem
BILDER = {"jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp", "gif"}
HEIC = {"heic", "heif"}
TEXTDATEIEN = {"txt", "csv", "tsv", "md", "json", "xml", "log", "ini", "conf", "cfg", "yml", "yaml", "sh"}
TABELLEN = {"xlsx", "xlsm", "xls"}
# ohne Texterkennung, also schnell – lässt sich jederzeit nachlesen
OHNE_OCR = TEXTDATEIEN | TABELLEN | {"docx", "doc"}


@dataclass
class Ergebnis:
    text: str
    ablage_datei: Path
    endung: str
    ocr: bool = False
    fehler: str | None = None


def _lauf(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "Zeitlimit")
    except OSError:
        return subprocess.CompletedProcess(args, 127, "", "Programm fehlt")


def _seiten(pfad: Path) -> int:
    r = _lauf(["pdfinfo", str(pfad)], timeout=60)
    m = re.search(r"^Pages:\s+(\d+)", r.stdout or "", re.M)
    return max(1, int(m.group(1))) if m else 1


def _pdftext(pfad: Path) -> str | None:
    r = _lauf(["pdftotext", "-layout", str(pfad), "-"], timeout=300)
    if r.returncode != 0:
        return None
    return (r.stdout or "")[:MAX_TEXT]


def _zu_wenig(text: str | None, seiten: int) -> bool:
    n = len(re.sub(r"\s+", "", text or ""))
    return n / max(1, seiten) < MIN_ZEICHEN_JE_SEITE


def _pdf(pfad: Path, arbeit: Path) -> Ergebnis:
    text = _pdftext(pfad)
    seiten = _seiten(pfad)
    if not _zu_wenig(text, seiten):
        return Ergebnis(text or "", pfad, "pdf")
    ziel = arbeit / f"{pfad.stem[:40]}-ocr.pdf"
    r = _lauf(["ocrmypdf", "-l", SPRACHEN, "--skip-text", "--output-type", "pdf",
               "--jobs", "2", "--optimize", "1", str(pfad), str(ziel)], timeout=1800)
    if r.returncode != 0 or not ziel.exists():
        ziel.unlink(missing_ok=True)
        grund = "PDF nicht lesbar" if text is None else "Texterkennung fehlgeschlagen"
        return Ergebnis(text or "", pfad, "pdf", fehler=f"{grund} (Code {r.returncode})")
    return Ergebnis(_pdftext(ziel) or "", ziel, "pdf", ocr=True)


def _bild(pfad: Path, arbeit: Path, endung: str) -> Ergebnis:
    quelle = pfad
    if endung in HEIC:
        jpg = arbeit / f"{pfad.stem[:40]}.jpg"
        r = _lauf(["heif-convert", str(pfad), str(jpg)], timeout=300)
        if r.returncode != 0 or not jpg.exists():
            return Ergebnis("", pfad, endung, fehler=f"HEIC nicht lesbar (Code {r.returncode})")
        quelle = jpg
    basis = arbeit / f"{pfad.stem[:40]}-scan"
    r = _lauf(["tesseract", str(quelle), str(basis), "-l", SPRACHEN, "pdf"], timeout=1800)
    ziel = basis.with_suffix(".pdf")
    if quelle != pfad:
        quelle.unlink(missing_ok=True)
    if r.returncode != 0 or not ziel.exists():
        return Ergebnis("", pfad, endung, fehler=f"Texterkennung fehlgeschlagen (Code {r.returncode})")
    return Ergebnis(_pdftext(ziel) or "", ziel, "pdf", ocr=True)


def _docx(pfad: Path) -> str:
    import docx
    d = docx.Document(str(pfad))
    teile = [p.text for p in d.paragraphs]
    for t in d.tables:
        for zeile in t.rows:
            teile.append(" | ".join(c.text for c in zeile.cells))
    return "\n".join(teile)


_NUR_ZEIT_TAGE = {dt.date(1899, 12, 30), dt.date(1899, 12, 31), dt.date(1900, 1, 1)}   # Excel-Tag 0


def _zelle(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, dt.datetime):
        if v.date() in _NUR_ZEIT_TAGE:
            return v.strftime("%H:%M")
        return v.strftime("%d.%m.%Y") if not v.hour and not v.minute else v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, dt.date):
        return v.strftime("%d.%m.%Y")
    return " ".join(str(v).split())


def _tabellenzeilen(blaetter) -> str:
    """Zellen mit Tabulator getrennt (Spalten bleiben für die Tabellen-Ansicht erhalten)."""
    teile, n, zellen = [], 0, 0
    for zeilen in blaetter:
        for zeile in zeilen:
            zeile = list(zeile)
            zellen += max(1, len(zeile))
            if zellen > MAX_ZELLEN:
                return "\n".join(teile)
            werte = [_zelle(v) for v in zeile]
            while werte and not werte[-1]:
                werte.pop()
            if not werte:
                continue
            z = "\t".join(werte)
            teile.append(z)
            n += len(z)
            if n > MAX_TEXT:
                return "\n".join(teile)
    return "\n".join(teile)


def _xlsx(pfad: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(pfad), read_only=True, data_only=True)
    try:
        blaetter = wb.worksheets[:3]
        for ws in blaetter:
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()   # gelogene Blattgröße (A1:XFD1048576) nicht glauben
        return _tabellenzeilen(ws.iter_rows(values_only=True) for ws in blaetter)
    finally:
        wb.close()


def _xls(pfad: Path) -> str:
    """Altes Excel-Format (bis Office 2003)."""
    import xlrd
    wb = xlrd.open_workbook(str(pfad), on_demand=True, ragged_rows=True)

    def zeilen(ws):
        for r in range(ws.nrows):
            werte = []
            for c in ws.row(r):
                if c.ctype == xlrd.XL_CELL_DATE:
                    try:
                        werte.append(xlrd.xldate_as_datetime(c.value, wb.datemode))
                        continue
                    except (ValueError, OverflowError, xlrd.xldate.XLDateError):
                        pass
                werte.append(c.value if c.ctype not in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK,
                                                        xlrd.XL_CELL_ERROR) else None)
            yield werte
    try:
        def blaetter():
            for i in range(min(3, wb.nsheets)):
                yield zeilen(wb.sheet_by_index(i))
                wb.unload_sheet(i)
        return _tabellenzeilen(blaetter())
    finally:
        wb.release_resources()


def _doc(pfad: Path) -> str:
    """Altes Word-Format (bis Office 2003) per catdoc."""
    # catdoc liest fremde Dateien: Speicher und Rechenzeit hart begrenzen
    r = _lauf(["prlimit", "--as=536870912", "--cpu=60", "--", "catdoc", "-w", "-d", "utf-8", str(pfad)],
              timeout=120)
    if r.returncode != 0:
        raise ValueError("catdoc")
    return (r.stdout or "")[:MAX_TEXT]


def _txt(pfad: Path) -> str:
    with open(pfad, "rb") as f:
        roh = f.read(MAX_TEXT)
    try:
        return roh.decode("utf-8")
    except UnicodeDecodeError as e:
        if len(roh) >= MAX_TEXT and e.start >= len(roh) - 3:   # nur am Schnitt ein Zeichen zerteilt
            try:
                return roh[:e.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
        return roh.decode("cp1252", errors="replace")


def lies(pfad: Path, arbeitsordner: Path) -> Ergebnis:
    pfad = Path(pfad)
    endung = pfad.suffix.lower().lstrip(".")
    try:
        if endung == "pdf":
            return _pdf(pfad, arbeitsordner)
        if endung in BILDER or endung in HEIC:
            return _bild(pfad, arbeitsordner, endung)
        if endung == "docx":
            return Ergebnis(_docx(pfad)[:MAX_TEXT], pfad, endung)
        if endung == "doc":
            return Ergebnis(_doc(pfad)[:MAX_TEXT], pfad, endung)
        if endung in ("xlsx", "xlsm"):
            return Ergebnis(_xlsx(pfad)[:MAX_TEXT], pfad, endung)
        if endung == "xls":
            return Ergebnis(_xls(pfad)[:MAX_TEXT], pfad, endung)
        if endung in TEXTDATEIEN:
            return Ergebnis(_txt(pfad), pfad, endung)
        return Ergebnis("", pfad, endung or "bin")
    except Exception as e:  # kaputte Datei: trotzdem ablegen
        return Ergebnis("", pfad, endung or "bin", fehler=f"Datei nicht lesbar ({type(e).__name__})")
