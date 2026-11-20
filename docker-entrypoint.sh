#!/bin/sh
# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
set -e
# Datenbank, Papierkorb, Dubletten: nur für den Besitzer und seine Gruppe lesbar
umask 027
mkdir -p "$DATA_DIR"
cd /app
# Kein Zugriffsprotokoll: Such- und Filterwerte (Absender, Begriffe) stünden sonst im Docker-Log.
exec uvicorn server:app --host 0.0.0.0 --port 8080 --no-server-header --no-access-log
