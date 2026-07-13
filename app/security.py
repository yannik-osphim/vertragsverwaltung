import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from .config import load_env

load_env()

SESSION_COOKIE_NAME = "invoice_session"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("SESSION_MAX_AGE_SECONDS", "28800"))
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-change-this-session-secret")
HASH_ITERATIONS = 310_000
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!#$%&()*+-./:;<=>?@[]^_{|}~"


def generate_password(length: int = 64) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        HASH_ITERATIONS,
    )
    return f"pbkdf2_sha256${HASH_ITERATIONS}${_b64_encode(salt)}${_b64_encode(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64_decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(_b64_encode(digest), expected)
    except (ValueError, TypeError):
        return False


def _sign(payload: str) -> str:
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64_encode(signature)


def create_session_token(data: dict[str, Any]) -> str:
    payload = dict(data)
    payload["iat"] = int(time.time())
    encoded_payload = _b64_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{encoded_payload}.{_sign(encoded_payload)}"


def read_session_token(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None

    encoded_payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded_payload), signature):
        return None

    try:
        payload = json.loads(_b64_decode(encoded_payload))
    except (ValueError, TypeError):
        return None

    issued_at = payload.get("iat")
    if not isinstance(issued_at, int):
        return None
    if time.time() - issued_at > SESSION_MAX_AGE_SECONDS:
        return None

    return payload
