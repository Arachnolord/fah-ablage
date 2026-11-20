# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Anmeldung (übernommen aus dem FAH-Downloader).

Ein Admin in DATA_DIR/users.json, Passwort nur als PBKDF2-Hash, Sitzung als
signiertes Cookie (Schlüssel in DATA_DIR/secret.key, überlebt Updates).
"""
import hashlib
import json
import hmac
import secrets
import threading
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

import config

SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 Tage
_setup_lock = threading.Lock()
_DUMMY_SALT = "00" * 16
_serializer = None


class SchonEingerichtet(RuntimeError):
    """Es gibt bereits einen Admin – die Ersteinrichtung ist vorbei."""


def _ser():
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.get_secret(), salt="ablage-session")
    return _serializer


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


class ZugangKaputt(RuntimeError):
    """users.json existiert, ist aber nicht lesbar – dann nie die Einrichtung öffnen."""


def _users() -> dict:
    try:
        roh = config.USERS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"users": {}}
    except OSError:
        raise ZugangKaputt() from None
    try:
        d = json.loads(roh)
        if not isinstance(d, dict) or not isinstance(d.get("users", {}), dict):
            raise ValueError
        return d
    except ValueError:
        raise ZugangKaputt() from None


def zugang_lesbar() -> bool:
    try:
        _users()
        return True
    except ZugangKaputt:
        return False


def has_admin() -> bool:
    return bool(_users().get("users"))


def create_user(username: str, password: str) -> None:
    username = (username or "").strip()
    if not username or len(password or "") < 8:
        raise ValueError("Benutzername und ein Passwort mit mindestens 8 Zeichen sind nötig.")
    data = _users()
    salt = secrets.token_hex(16)
    data.setdefault("users", {})[username] = {"salt": salt, "hash": _hash_pw(password, salt),
                                              "created": int(time.time())}
    config.save_json(config.USERS_FILE, data)


def setup_admin(username: str, password: str) -> None:
    with _setup_lock:
        if config.USERS_FILE.exists() or has_admin():
            raise SchonEingerichtet()
        create_user(username, password)


def verify(username: str, password: str) -> bool:
    u = _users().get("users", {}).get((username or "").strip())
    if not u:
        _hash_pw(password or "", _DUMMY_SALT)
        return False
    return hmac.compare_digest(u["hash"], _hash_pw(password or "", u["salt"]))


def make_session(username: str) -> str:
    return _ser().dumps({"u": username})


def read_session(token: str):
    if not token:
        return None
    try:
        u = _ser().loads(token, max_age=SESSION_MAX_AGE).get("u")
    except (BadSignature, Exception):
        return None
    return u if u in _users().get("users", {}) else None
