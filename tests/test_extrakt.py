# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
import subprocess
from pathlib import Path

import docx
import pytest

import extrakt


class FakeLauf:
    """Ersetzt pdftotext/pdfinfo/ocrmypdf/tesseract/heif-convert."""

    def __init__(self, text_vorher="", text_nachher="Beitragsbescheid TK " * 20, seiten=1,
                 ocr_rc=0, pdftotext_rc=0):
        self.aufrufe = []
        self.text_vorher, self.text_nachher = text_vorher, text_nachher
        self.seiten, self.ocr_rc, self.pdftotext_rc = seiten, ocr_rc, pdftotext_rc
        self.ocr_fertig = False

    def __call__(self, args, timeout=600):
        self.aufrufe.append(args)
        prog = args[0]
        out, rc = "", 0
        if prog == "pdfinfo":
            out = f"Pages:          {self.seiten}\n"
        elif prog == "pdftotext":
            rc = self.pdftotext_rc
            out = self.text_nachher if self.ocr_fertig else self.text_vorher
        elif prog == "ocrmypdf":
            rc = self.ocr_rc
            if rc == 0:
                Path(args[-1]).write_bytes(b"%PDF-ocr")
                self.ocr_fertig = True
        elif prog == "tesseract":
            Path(args[2] + ".pdf").write_bytes(b"%PDF-tess")
            self.ocr_fertig = True
        elif prog == "heif-convert":
            Path(args[-1]).write_bytes(b"jpg")
        return subprocess.CompletedProcess(args, rc, out, "")

    def programme(self):
        return [a[0] for a in self.aufrufe]


@pytest.fixture
def arbeit(tmp_path):
    a = tmp_path / "arbeit"
    a.mkdir()
    return a


def test_text_pdf_ohne_ocr(tmp_path, arbeit, monkeypatch):
    f = FakeLauf(text_vorher="Techniker Krankenkasse Beitragsbescheid " * 10)
    monkeypatch.setattr(extrakt, "_lauf", f)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF")
    e = extrakt.lies(p, arbeit)
    assert "Beitragsbescheid" in e.text and e.ocr is False and e.fehler is None
    assert e.ablage_datei == p and e.endung == "pdf"
    assert "ocrmypdf" not in f.programme()


def test_scan_pdf_mit_ocr_deutsch(tmp_path, arbeit, monkeypatch):
    f = FakeLauf(text_vorher="  \n", seiten=2)
    monkeypatch.setattr(extrakt, "_lauf", f)
    p = tmp_path / "scan.PDF"
    p.write_bytes(b"%PDF")
    e = extrakt.lies(p, arbeit)
    ocr = [a for a in f.aufrufe if a[0] == "ocrmypdf"][0]
    assert "deu" in ocr and "--skip-text" in ocr and ocr[ocr.index("--jobs") + 1] == "2"
    assert e.ocr is True and "Beitragsbescheid" in e.text
    assert e.ablage_datei.parent == arbeit and e.ablage_datei.exists()


def test_ocr_fehler_original_ablegen(tmp_path, arbeit, monkeypatch):
    f = FakeLauf(text_vorher="", ocr_rc=2)
    monkeypatch.setattr(extrakt, "_lauf", f)
    p = tmp_path / "kaputt.pdf"
    p.write_bytes(b"%PDF")
    e = extrakt.lies(p, arbeit)
    assert e.fehler and e.ablage_datei == p and e.ocr is False


def test_pdf_nicht_lesbar(tmp_path, arbeit, monkeypatch):
    f = FakeLauf(pdftotext_rc=1, ocr_rc=8)
    monkeypatch.setattr(extrakt, "_lauf", f)
    p = tmp_path / "verschluesselt.pdf"
    p.write_bytes(b"%PDF")
    e = extrakt.lies(p, arbeit)
    assert e.fehler and e.ablage_datei == p and e.text == ""


def test_bild_wird_pdf_per_tesseract(tmp_path, arbeit, monkeypatch):
    f = FakeLauf()
    monkeypatch.setattr(extrakt, "_lauf", f)
    p = tmp_path / "foto.jpg"
    p.write_bytes(b"jpg")
    e = extrakt.lies(p, arbeit)
    t = [a for a in f.aufrufe if a[0] == "tesseract"][0]
    assert t[-1] == "pdf" and "deu" in t
    assert e.endung == "pdf" and e.ocr is True and e.ablage_datei.suffix == ".pdf"


def test_heic_erst_umwandeln(tmp_path, arbeit, monkeypatch):
    f = FakeLauf()
    monkeypatch.setattr(extrakt, "_lauf", f)
    p = tmp_path / "IMG_1.HEIC"
    p.write_bytes(b"heic")
    e = extrakt.lies(p, arbeit)
    assert f.programme()[:2] == ["heif-convert", "tesseract"]
    assert e.endung == "pdf"


def test_docx_und_txt_echt(tmp_path, arbeit):
    d = docx.Document()
    d.add_paragraph("Mietvertrag Wohnung Musterstadt")
    p = tmp_path / "vertrag.docx"
    d.save(p)
    e = extrakt.lies(p, arbeit)
    assert "Mietvertrag" in e.text and e.endung == "docx" and e.ablage_datei == p
    t = tmp_path / "notiz.txt"
    t.write_bytes("Größe Übersicht".encode("cp1252"))
    assert "Größe" in extrakt.lies(t, arbeit).text


def test_unbekannte_endung(tmp_path, arbeit):
    p = tmp_path / "alt.xyz"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    e = extrakt.lies(p, arbeit)
    assert e.text == "" and e.fehler is None and e.endung == "xyz"


def test_xls_altes_excel_echt(tmp_path, arbeit):
    xlwt = pytest.importorskip("xlwt")
    import datetime
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Zeiten")
    ws.write(0, 0, "Datum"); ws.write(0, 1, "Stunden"); ws.write(0, 3, "Notiz")
    ws.write(1, 0, datetime.datetime(2024, 12, 1), xlwt.easyxf(num_format_str="DD.MM.YYYY"))
    ws.write(1, 1, 7.5); ws.write(1, 3, "Frühdienst")
    ws.write(2, 1, 8.0)
    p = tmp_path / "zeiten.xls"
    wb.save(str(p))
    e = extrakt.lies(p, arbeit)
    assert e.fehler is None and e.endung == "xls" and e.ablage_datei == p
    assert e.text.splitlines() == ["Datum\tStunden\t\tNotiz", "01.12.2024\t7.5\t\tFrühdienst", "\t8"]


def test_xlsx_spalten_per_tabulator(tmp_path, arbeit):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Monat", None, "Betrag"]); ws.append(["Januar", None, 12.0]); ws.append([None, None, None])
    p = tmp_path / "t.xlsx"
    wb.save(p)
    assert extrakt.lies(p, arbeit).text.splitlines() == ["Monat\t\tBetrag", "Januar\t\t12"]


def test_doc_altes_word_per_catdoc(tmp_path, arbeit, monkeypatch):
    aufrufe = []

    def lauf(args, timeout=600):
        aufrufe.append(args)
        return subprocess.CompletedProcess(args, 0, "Kündigung zum 31.12.\n", "")
    monkeypatch.setattr(extrakt, "_lauf", lauf)
    p = tmp_path / "alt.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    e = extrakt.lies(p, arbeit)
    assert aufrufe[0][:1] == ["prlimit"] and "catdoc" in aufrufe[0] and "Kündigung" in e.text
    assert e.ablage_datei == p
    monkeypatch.setattr(extrakt, "_lauf", lambda a, timeout=600: subprocess.CompletedProcess(a, 1, "", ""))
    e = extrakt.lies(p, arbeit)
    assert e.text == "" and e.fehler and "alt" not in e.fehler


def test_zeiten_und_zellgrenze(tmp_path, arbeit, monkeypatch):
    import datetime
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Beginn", datetime.time(8, 30), datetime.datetime(2024, 12, 1), datetime.datetime(2024, 12, 1, 14, 5)])
    for i in range(50):
        ws.append([f"z{i}", i])
    p = tmp_path / "t.xlsx"
    wb.save(p)
    zeilen = extrakt.lies(p, arbeit).text.splitlines()
    assert zeilen[0] == "Beginn\t08:30\t01.12.2024\t01.12.2024 14:05"
    monkeypatch.setattr(extrakt, "MAX_ZELLEN", 20)
    assert len(extrakt.lies(p, arbeit).text.splitlines()) < 12
    xlwt = pytest.importorskip("xlwt")
    wb2 = xlwt.Workbook()
    wb2.add_sheet("A").write(0, 0, 0.35, xlwt.easyxf(num_format_str="HH:MM"))
    wb2.save(str(tmp_path / "z.xls"))
    assert extrakt.lies(tmp_path / "z.xls", arbeit).text == "08:24"


def test_utf8_am_schnitt_nicht_verdorben(tmp_path, arbeit, monkeypatch):
    monkeypatch.setattr(extrakt, "MAX_TEXT", 20)
    t = tmp_path / "gross.log"
    t.write_bytes("Grüße aus München und mehr".encode("utf-8"))   # Schnitt mitten im „ü“ von München
    text = extrakt.lies(t, arbeit).text
    assert text.startswith("Grüße aus M") and "Ã" not in text


def test_json_und_konfig_als_text(tmp_path, arbeit):
    for name in ("a.json", "b.yaml", "c.log"):
        (tmp_path / name).write_text('{"schluessel": "wert"}')
        assert "schluessel" in extrakt.lies(tmp_path / name, arbeit).text


def test_txt_liest_nicht_alles(tmp_path, arbeit, monkeypatch):
    t = tmp_path / "riesig.txt"
    t.write_bytes(b"a" * 5000)
    monkeypatch.setattr(extrakt, "MAX_TEXT", 1000)

    def verboten(self):
        raise AssertionError("ganze Datei gelesen")
    monkeypatch.setattr(Path, "read_bytes", verboten)
    assert len(extrakt.lies(t, arbeit).text) == 1000
