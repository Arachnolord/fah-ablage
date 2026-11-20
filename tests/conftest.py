# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Gemeinsame Test-Hilfen: app/ importierbar, Pfade auf Tmp-Ordner biegen."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ablage-test-"))
for name in ("config", "eingang", "archiv", "import"):
    (_TMP / name).mkdir()
os.environ.setdefault("DATA_DIR", str(_TMP / "config"))
os.environ.setdefault("EINGANG_DIR", str(_TMP / "eingang"))
os.environ.setdefault("ARCHIV_DIR", str(_TMP / "archiv"))
os.environ.setdefault("IMPORT_DIR", str(_TMP / "import"))
os.environ["ABLAGE_KEIN_AUTOSTART"] = "1"
os.environ.setdefault("ABLAGE_HOSTS", "testserver")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
