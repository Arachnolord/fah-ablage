#!/bin/sh
# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
#
# manage.sh – FAH-Ablage auf dem NAS
#   ./manage.sh update [zip]   neueste fah-ablage*.zip einspielen (höchste (N) gewinnt), bauen, starten
#   ./manage.sh start|stop|restart|status|logs
#
# Nie angefasst: config/ (Datenbank, Zugang), modelle/, .env, docker-compose.override.yml
# und natürlich Eingang/Archiv/Altbestand.
#
# Das ganze Skript steckt in main(): die Shell hat es vollständig gelesen,
# bevor update die Datei selbst ersetzt.

main() {
    set -e
    DIR="$(cd "$(dirname "$0")" && pwd)"
    cd "$DIR"
    ZIPNAME="fah-ablage.zip"
    case "${1:-}" in
        update)  shift; update "$@" ;;
        _fertig) fertigstellen ;;
        start)   vorbereiten; docker compose up -d ;;
        stop)    docker compose down ;;
        restart) vorbereiten; docker compose up -d --force-recreate ;;
        status)  docker compose ps; echo "WebGUI: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(wert ABLAGE_PORT)" ;;
        logs)    docker compose logs -f --tail 100 ;;
        *)       echo "Verwendung: $0 update [zip] | start | stop | restart | status | logs"; exit 1 ;;
    esac
}

wert() { sed -n "s/^$1=//p" .env | tail -1; }

setze_wert() {
    if grep -q "^$1=" .env; then
        sed -i "s|^$1=.*|$1=$2|" .env
    else
        echo "$1=$2" >> .env
    fi
}

port_frei() {
    python3 -c "
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('0.0.0.0', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()" "$1"
}

port_pruefen() {
    port="$(wert ABLAGE_PORT)"
    [ -n "$port" ] || port=8093
    # Gehört der Port schon unserem eigenen Container, ist alles gut.
    if docker ps --filter "name=^fah-ablage$" --format '{{.Ports}}' 2>/dev/null | grep -oE ":[0-9]+->" | grep -q ":$port->"; then
        return 0
    fi
    if port_frei "$port"; then
        return 0
    fi
    neu=$((port + 1))
    while [ $neu -lt $((port + 100)) ]; do
        if port_frei "$neu"; then
            echo "[manage] Port $port ist belegt – weiche auf $neu aus."
            setze_wert ABLAGE_PORT "$neu"
            return 0
        fi
        neu=$((neu + 1))
    done
    echo "[manage] Kein freier Port gefunden."; exit 1
}

netz_pruefen() {
    for n in $(docker network ls -q); do
        name="$(docker network inspect -f '{{.Name}}' "$n")"
        [ "$name" = "fah-ablage_default" ] && continue
        if docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$n" | grep -q "172.16.41.0/24"; then
            echo "[manage] Subnetz 172.16.41.0/24 ist schon vom Netz '$name' belegt – bitte in docker-compose.yml ein freies 172.16.x.0/24 eintragen."
            exit 1
        fi
    done
}

vorbereiten() {
    [ -f .env ] || cp .env.example .env
    # Seit 1.6: Import-Ordner frei wählbar unterhalb des persönlichen Ordners (nur lesend eingebunden).
    # Nie "/" oder ein Pfad mit nur einem Teil (/volume1) – dann bleibt es beim bisherigen Altbestand-Ordner.
    if [ -z "$(wert QUELLEN_DIR)" ]; then
        q="$(getent passwd "$(wert PUID)" 2>/dev/null | cut -d: -f6)"
        case "$q" in /?*/?*) [ -d "$q" ] || q="" ;; *) q="" ;; esac
        [ -n "$q" ] || q="$(wert IMPORT_DIR)"
        echo "[manage] Import-Ordner wählbar unter: $q"
        setze_wert QUELLEN_DIR "$q"
    fi
    # Immer aus der Vorlage – eigene Anpassungen gehören in docker-compose.override.yml.
    cp docker-compose.yml.example docker-compose.yml
    mkdir -p config modelle "$(wert EINGANG_DIR)" "$(wert ARCHIV_DIR)"
    if [ "$(id -u)" = 0 ]; then
        chown "$(wert PUID):$(wert PGID)" config "$(wert EINGANG_DIR)" "$(wert ARCHIV_DIR)"
    fi
    if [ ! -s modelle/qwen3-4b.gguf ]; then
        q="$(wert MODELL_QUELLE)"
        if [ -s "$q" ]; then
            echo "[manage] Kopiere das Sprachmodell (einmalig, ~2,5 GB) …"
            cp "$q" modelle/qwen3-4b.gguf.teil && mv modelle/qwen3-4b.gguf.teil modelle/qwen3-4b.gguf
        else
            echo "[manage] Sprachmodell fehlt: $q – bitte MODELL_QUELLE in .env anpassen."; exit 1
        fi
    fi
    netz_pruefen
    port_pruefen
}

update() {
    zip="${1:-}"
    [ -n "$zip" ] || zip="$(python3 tools/release_zip.py latest "$DIR" "$ZIPNAME" 2>/dev/null || true)"
    if [ -z "$zip" ] && [ -f "$DIR/$ZIPNAME" ]; then zip="$DIR/$ZIPNAME"; fi
    [ -n "$zip" ] && [ -f "$zip" ] || { echo "[manage] Keine $ZIPNAME gefunden."; exit 1; }
    echo "[manage] Spiele ein: $(basename "$zip")"
    python3 - "$zip" "$DIR" <<'PY'
import os, sys, zipfile
zip_pfad, ziel = sys.argv[1], sys.argv[2]
SCHUTZ = ("config/", "modelle/", ".env", "docker-compose.yml")
with zipfile.ZipFile(zip_pfad) as z:
    for info in z.infolist():
        name = os.path.normpath(info.filename).replace(os.sep, "/")
        if name.startswith("/") or name == "." or ".." in name.split("/"):
            continue
        if not os.path.realpath(os.path.join(ziel, name)).startswith(os.path.realpath(ziel) + os.sep):
            continue
        if name == ".env" or name == "docker-compose.yml" or name.startswith(("config/", "modelle/")):
            continue
        pfad = os.path.join(ziel, name)
        if info.is_dir():
            os.makedirs(pfad, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(pfad), exist_ok=True)
        tmp = pfad + ".neu"
        with z.open(info) as q, open(tmp, "wb") as o:
            o.write(q.read())
        modus = (info.external_attr >> 16) & 0o777
        os.chmod(tmp, modus or 0o644)
        os.replace(tmp, pfad)
PY
    chmod 755 manage.sh docker-entrypoint.sh 2>/dev/null || true
    python3 tools/release_zip.py cleanup "$DIR" "$ZIPNAME" "$zip" >/dev/null 2>&1 || true
    # Weiter mit dem GERADE eingespielten manage.sh – sonst gelten neue .env-Werte erst beim nächsten Update.
    exec sh "$DIR/manage.sh" _fertig
}

fertigstellen() {
    vorbereiten
    docker compose up -d --build --force-recreate
    echo "[manage] Fertig. WebGUI: Port $(wert ABLAGE_PORT)"
}

main "$@"; exit $?
