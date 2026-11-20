#!/bin/sh
# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
# Baut fah-ablage.zip aus den versionierten Dateien (git ls-files), ohne Tests/Doku.
set -e
cd "$(dirname "$0")/.."
rm -f fah-ablage.zip
git ls-files -- app tools Dockerfile docker-entrypoint.sh docker-compose.yml.example .env.example manage.sh \
    | grep -v '^tools/baue_zip.sh$' \
    | zip -q -X fah-ablage.zip -@
unzip -l fah-ablage.zip | tail -1
shasum -a 256 fah-ablage.zip
