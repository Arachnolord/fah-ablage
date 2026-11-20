# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
# FAH-Ablage – Weboberfläche + Arbeiter. Die KI läuft im zweiten Container.
FROM debian:trixie-slim

# Texterkennung (Deutsch + Englisch), PDF-Werkzeuge, HEIC-Umwandlung
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates tzdata \
        ocrmypdf tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
        poppler-utils ghostscript libheif-examples catdoc \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY tools/probelauf_zahlen.py tools/beispiele.py /app/werkzeuge/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod 755 /docker-entrypoint.sh

ENV DATA_DIR=/config EINGANG_DIR=/eingang ARCHIV_DIR=/archiv IMPORT_DIR=/import \
    HOME=/tmp OMP_THREAD_LIMIT=1
EXPOSE 8080
ENTRYPOINT ["/docker-entrypoint.sh"]
