"""Minimal HS256 JWTs for the emulated issuers (Google, Entra, JFrog)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


class JwtError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode(claims: dict, key: bytes) -> str:
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def claims(token: str) -> dict:
    """Payload without verification (for inspection only)."""
    try:
        return json.loads(_unb64(token.split(".")[1]))
    except (IndexError, ValueError) as exc:
        raise JwtError("not a JWT") from exc


def decode(token: str, key: bytes) -> dict:
    """Verify the HS256 signature and return the payload. Time claims are the caller's job."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtError("not a JWT")
    want = hmac.new(key, f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
    try:
        got = _unb64(parts[2])
    except ValueError as exc:
        raise JwtError("bad signature encoding") from exc
    if not hmac.compare_digest(want, got):
        raise JwtError("signature verification failed")
    return claims(token)
