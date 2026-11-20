# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
import subprocess
from pathlib import Path

import vorschau


def _fake_lauf(aufrufe, rc=0):
    def lauf(args, timeout=120):
        aufrufe.append(args)
        if rc == 0:
            # pdftoppm schreibt <präfix>.png bei -singlefile
            Path(args[-1] + ".png").write_bytes(b"\x89PNG-fake")
        return subprocess.CompletedProcess(args, rc, "", "")
    return lauf


def test_pdf_vorschau_wird_erzeugt_und_wiederverwendet(tmp_path, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(vorschau, "_lauf", _fake_lauf(aufrufe))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    ziel = tmp_path / "vorschau"
    p = vorschau.bild(7, pdf, ziel)
    assert p == ziel / "7.png" and p.read_bytes() == b"\x89PNG-fake"
    assert aufrufe[0][0] == "pdftoppm" and "-singlefile" in aufrufe[0] and "-l" in aufrufe[0]
    assert vorschau.bild(7, pdf, ziel) == p
    assert len(aufrufe) == 1                      # zweites Mal aus dem Zwischenspeicher


def test_pdf_fehler_wird_nicht_gemerkt(tmp_path, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(vorschau, "_lauf", _fake_lauf(aufrufe, rc=1))
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF")
    assert vorschau.bild(2, pdf, tmp_path / "v") is None
    assert not list((tmp_path / "v").iterdir())
    assert vorschau.bild(2, tmp_path / "fehlt.csv", tmp_path / "v", text_holen=lambda: "a;b") is None


def test_textblatt_fuer_csv_mit_escaping(tmp_path, monkeypatch):
    import xml.dom.minidom
    aufrufe = []
    monkeypatch.setattr(vorschau, "_lauf", _fake_lauf(aufrufe))
    csv = tmp_path / "a.csv"
    csv.write_bytes(b"x")
    text = "Datum;Betrag\n<script>alert(1)</script>&\x00\x07\n\n\n" + "Z" * 80 + "\n" + "\n".join(f"z{i}" for i in range(40))
    p = vorschau.bild(5, csv, tmp_path / "v", text_holen=lambda: text)
    assert p == tmp_path / "v" / "5.svg" and not aufrufe              # kein pdftoppm
    inhalt = p.read_text()
    xml.dom.minidom.parseString(inhalt)                                 # gültiges XML trotz Steuerzeichen
    assert "<script" not in inhalt and "&lt;script&gt;" in inhalt and "Datum;Betrag" in inhalt
    assert inhalt.count("<text ") == vorschau.ZEILEN and "Z" * vorschau.SPALTEN + "…" in inhalt
    assert vorschau.bild(5, csv, tmp_path / "v", text_holen=lambda: "anders") == p   # gemerkt


def test_textblatt_ohne_text_zeigt_hinweis(tmp_path):
    p = vorschau.bild(6, Path(__file__), tmp_path / "v", text_holen=lambda: "  \n ")
    assert "Kein Text gefunden" in p.read_text() and ">PY<" in p.read_text()


def test_ansicht_csv_tabelle_und_text():
    a = vorschau.ansicht("csv", "Datum;Betrag;Text\n01.01.2024;12,50;\"a;b\"\n")
    assert a["vorschau_tabelle"] == [["Datum", "Betrag", "Text"], ["01.01.2024", "12,50", "a;b"]]
    assert a["vorschau_gekuerzt"] is False
    viele = "a,b\n" * (vorschau.CSV_ZEILEN + 10)
    assert vorschau.ansicht("csv", viele)["vorschau_gekuerzt"] is True
    assert vorschau.ansicht("csv", "nur eine spalte\nnoch eine") == {"vorschau_text": "nur eine spalte\nnoch eine",
                                                                       "vorschau_gekuerzt": False}
    assert vorschau.ansicht("docx", "Hallo") == {"vorschau_text": "Hallo", "vorschau_gekuerzt": False}
    assert vorschau.ansicht("txt", "x" * (vorschau.TEXT_MAX + 1))["vorschau_gekuerzt"] is True
    assert vorschau.ansicht("bin", "") == {}


def test_vorschau_loeschen(tmp_path, monkeypatch):
    monkeypatch.setattr(vorschau, "_lauf", _fake_lauf([]))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    p = vorschau.bild(3, pdf, tmp_path / "v")
    vorschau.loeschen(3, tmp_path / "v")
    assert not p.exists()
    t = tmp_path / "a.txt"
    t.write_text("x")
    s = vorschau.bild(4, t, tmp_path / "v", text_holen=lambda: "x")
    vorschau.loeschen(4, tmp_path / "v")
    assert not s.exists()
