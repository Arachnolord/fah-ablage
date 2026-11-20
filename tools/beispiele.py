#!/usr/bin/env python3
# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Erfundene Beispielbriefe für den Funktionstest (keine echten Daten).

Aufruf im Container: python3 /app/werkzeuge/beispiele.py <zielordner>
Erzeugt 4 Text-PDFs und einen Bild-Scan (PNG über Ghostscript).
"""
import subprocess
import sys
from pathlib import Path

EMPF = ["Max Mustermann", "Musterstraße 1", "12345 Musterstadt", ""]

BRIEFE = {
    "brief1.pdf": ["Techniker Krankenkasse", "Postfach 12 34 56, 20000 Hamburg", ""] + EMPF + [
        "Hamburg, 12.11.2026", "", "Ihr Beitragsbescheid ab 01.01.2027", "",
        "Versichertennummer: X123456789", "Geburtsdatum: 01.01.1980", "",
        "Sehr geehrter Herr Mustermann,", "ab dem 1. Januar 2027 beträgt Ihr monatlicher Beitrag",
        "zur Kranken- und Pflegeversicherung 512,34 Euro.", "", "Mit freundlichen Grüßen", "Ihre TK"],
    "brief2.pdf": ["Finanzamt Kelheim", "Klosterstraße 1, 93309 Kelheim", ""] + EMPF + [
        "Datum: 03.06.2026", "", "Bescheid für 2025 über Einkommensteuer", "",
        "Steuernummer 123/456/78901", "Die Einkommensteuer wird festgesetzt auf 4.321,00 EUR.",
        "Erstattung: 812,00 EUR.", "", "Rechtsbehelfsbelehrung: Einspruch binnen eines Monats."],
    "brief3.pdf": ["Deutsche Rentenversicherung Bund", "10704 Berlin", ""] + EMPF + [
        "Berlin, 30. September 2026", "", "Renteninformation", "",
        "Versicherungsnummer 12 010180 M 123", "Ihre bisher erreichte Rentenanwartschaft",
        "entspräche einer monatlichen Rente von 1.234,56 Euro."],
    "brief4.pdf": ["Stadtwerke Musterstadt GmbH", "Werkstraße 5, 12345 Musterstadt", ""] + EMPF + [
        "Rechnungsdatum: 14.02.2026", "", "Jahresabrechnung Strom 2025", "",
        "Kundennummer 998877", "Verbrauch 2.345 kWh, Abschlag ab März 85,00 Euro monatlich."],
    "scan5.pdf": ["HUK-COBURG Versicherungsgruppe", "96444 Coburg", ""] + EMPF + [
        "Coburg, 05.10.2026", "", "Versicherungsschein Kfz-Haftpflicht", "",
        "Fahrzeug: M-AB 1234", "Beitrag 2027: 389,40 Euro jährlich."],
}


def _esc(s: str) -> bytes:
    b = s.encode("cp1252")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def pdf(zeilen: list[str]) -> bytes:
    inhalt = b"BT /F1 11 Tf 60 790 Td 15 TL " + b" ".join(b"(" + _esc(z) + b") Tj T*" for z in zeilen) + b" ET"
    objekte = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(inhalt)).encode() + b" >>\nstream\n" + inhalt + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    aus, lagen = b"%PDF-1.4\n", []
    for i, o in enumerate(objekte, 1):
        lagen.append(len(aus))
        aus += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(aus)
    aus += f"xref\n0 {len(objekte) + 1}\n0000000000 65535 f \n".encode()
    aus += b"".join(f"{l:010d} 00000 n \n".encode() for l in lagen)
    aus += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return aus


def main(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    for name, zeilen in BRIEFE.items():
        (ziel / name).write_bytes(pdf(zeilen))
    # Brief 5 als Foto-Scan (PNG ohne Textebene)
    subprocess.run(["gs", "-q", "-sDEVICE=pnggray", "-r150", "-o", str(ziel / "scan5.png"),
                    str(ziel / "scan5.pdf")], check=True)
    (ziel / "scan5.pdf").unlink()
    print("Beispiele:", len(list(ziel.iterdir())))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
