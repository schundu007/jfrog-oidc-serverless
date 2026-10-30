"""AWS Signature Version 4 for one request (stdlib only).

Used by AwsStsSource to call STS GetWebIdentityToken with the workload's own
temporary credentials. Nothing here logs or returns a secret: sign() returns
the headers to send, and the secret key only feeds the HMAC chain.

Checked against the published SigV4 examples (tests/test_sigv4.py).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Optional


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="-_.~")


def _canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(pairs))


def sign(method: str, url: str, headers: dict, body: bytes, *, access_key: str, secret_key: str,
         region: str, service: str, session_token: Optional[str] = None,
         now: Optional[float] = None) -> dict:
    """Headers for the request, including Authorization and X-Amz-Date (and the session token)."""
    parts = urllib.parse.urlsplit(url)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    date = stamp[:8]
    out = {k: v for k, v in headers.items()}
    out["Host"] = parts.netloc
    out["X-Amz-Date"] = stamp
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    canon = {k.lower(): " ".join(str(v).split()) for k, v in out.items()}
    signed = ";".join(sorted(canon))
    canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in sorted(canon))
    path = urllib.parse.quote(parts.path or "/", safe="/-_.~%")
    request = "\n".join([method.upper(), path, _canonical_query(parts.query), canonical_headers, signed,
                         hashlib.sha256(body or b"").hexdigest()])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
    sig = hmac.new(signing_key(secret_key, date, region, service), to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out
