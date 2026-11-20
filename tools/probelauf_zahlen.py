#!/usr/bin/env python3
# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Probelauf-Kennzahlen – gibt NUR Zahlen und Arten-Namen aus, nie Inhalte,
Dateinamen, Absender oder Ordnernamen. Aufruf im Container:
    python3 /app/werkzeuge/probelauf_zahlen.py
"""
import sqlite3
import statistics
import sys
from collections import Counter

DB = sys.argv[1] if len(sys.argv) > 1 else "/config/ablage.sqlite"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
z = {r["name"]: r["wert"] for r in c.execute("SELECT name, wert FROM zaehler")}
spalten = {r[1] for r in c.execute("PRAGMA table_info(dokumente)")}
extra = ", erst_grund, blick, blick_geklaert" if "erst_grund" in spalten else ""
docs = [dict(r) for r in c.execute("SELECT art, unsicher, grund, quelle, herkunft, sicherheit, ms" + extra +
                                    " FROM dokumente WHERE status='abgelegt'")]
warte = c.execute("SELECT quelle, COUNT(*) n FROM warteschlange GROUP BY quelle").fetchall()

print(f"Import: {z.get('import_fertig', 0)}/{z.get('import_gesamt', 0)}   Warteschlange: "
      + (", ".join(f"{r['quelle']}={r['n']}" for r in warte) or "leer"))
print(f"Abgelegt: {len(docs)}   Dubletten: {z.get('dubletten', 0)}   Fehler: {z.get('fehler', 0)}")
if not docs:
    sys.exit(0)
n = len(docs)
uns = sum(d["unsicher"] for d in docs)
print(f"Unsicher: {uns} ({uns / n:.0%})")
gruende = Counter()
for d in docs:
    for g in filter(None, (d["grund"] or "").split("; ")):
        gruende[g.split(" (")[0]] += 1
for g, k in gruende.most_common():
    print(f"  Grund {g}: {k}")
if extra:
    erst = Counter(g.split(" (")[0] for d in docs for g in filter(None, (d["erst_grund"] or "").split("; ")))
    for g, k in erst.most_common():
        print(f"  Grund beim Ablegen (seit 1.7) {g}: {k}")
    print(f"Zweiter Blick: geklärt {sum(d['blick_geklaert'] for d in docs)}, KI-Runde gelaufen {sum(d['blick'] for d in docs)}")
ms = [d["ms"] for d in docs if d["ms"]]
if ms:
    print(f"Sekunden je Dokument: Median {statistics.median(ms) / 1000:.0f}, Mittel {statistics.mean(ms) / 1000:.0f}, "
          f"Max {max(ms) / 1000:.0f}")
s = sorted(d["sicherheit"] for d in docs)
print("Sicherheit (10/25/50/75/90 %): " + " / ".join(f"{s[min(n - 1, int(n * q))]:.2f}" for q in (.1, .25, .5, .75, .9)))
print("Je Art:")
for art, k in Counter(d["art"] for d in docs).most_common():
    print(f"  {art}: {k}")
imp = [d for d in docs if d["quelle"] == "import"]
if imp:
    gruppen = {}
    for d in imp:
        gruppen.setdefault((d["herkunft"] or "-").split("/")[0], []).append(d["art"])
    passend = sum(Counter(a).most_common(1)[0][1] for a in gruppen.values())
    print(f"Einheitlichkeit (gleiche Art wie die Mehrheit im alten Ordner): {passend / len(imp):.0%} "
          f"über {len(gruppen)} alte Ordner")
    tief = {}
    for d in imp:
        tief.setdefault(d["herkunft"] or "-", []).append(d["art"])
    passend2 = sum(Counter(a).most_common(1)[0][1] for a in tief.values())
    print(f"Einheitlichkeit je Unterordner: {passend2 / len(imp):.0%} über {len(tief)} Unterordner")
