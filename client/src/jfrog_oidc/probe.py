"""Self-test for a workload's JFrog OIDC setup. Never returns or prints a token.

    from jfrog_oidc.probe import selftest
    selftest("generic-local/healthcheck/ping.txt")   # -> {"ok", "claims", "check", "read"}

    python -m jfrog_oidc.probe read generic-local/healthcheck/ping.txt
    python -m jfrog_oidc.probe selftest [<path>]

The samples serve selftest() at GET /healthz/jfrog, and the console's live
probes run the CLI as the target's identity. Output is one JSON line per
command with an "event" field, which is what the console parses from logs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
from typing import Optional

from .artifactory import _open
from .errors import JFrogOidcError
from .exchange import JFrogTokenProvider, _jwt_claims

# Claims worth comparing with the identity mapping; nothing else is returned.
CLAIM_KEYS = ("iss", "aud", "sub", "oid", "tid", "azp", "email", "ver", "iat", "exp")


def read(provider: JFrogTokenProvider, repo_path: str, timeout: float = 30.0) -> int:
    """GET <repo>/<path> with the exchanged token and return the HTTP status.

    The body is read and discarded; only the status matters.
    """
    url = f"{provider.jfrog_url}/artifactory/{urllib.parse.quote(repo_path.lstrip('/'))}"
    try:
        with _open(provider, url, timeout=timeout) as resp:
            while resp.read(64 * 1024):
                pass
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def selftest(
    artifact_path: Optional[str] = None,
    provider: Optional[JFrogTokenProvider] = None,
    fresh: bool = False,
) -> dict:
    """Check identity claims, the exchange and (optionally) an artifact read.

    fresh=True drops the cached JFrog token first, so the check exchanges again: it shows
    what JFrog answers to this identity now, not what it answered when the token was cached.

    Each part reports an "error" string instead of raising, so a health
    endpoint can always answer with what went wrong.
    """
    result: dict = {"ok": False, "claims": None, "check": None, "read": None}
    try:
        provider = provider or JFrogTokenProvider.from_env()
    except (JFrogOidcError, ValueError) as exc:
        result["check"] = {"error": str(exc)}
        return result

    try:
        claims = _jwt_claims(provider.source.get_token(provider.audience))
        result["claims"] = {k: claims[k] for k in CLAIM_KEYS if k in claims}
    except JFrogOidcError as exc:
        result["claims"] = {"error": str(exc)}

    if fresh:
        provider.invalidate()
    try:
        tok = provider.token()
    except JFrogOidcError as exc:
        result["check"] = {"error": str(exc), "provider": provider.provider_name}
        if exc.status:
            result["check"]["status"] = exc.status
        return result
    result["check"] = {
        "username": tok.username,
        "expires_in": int(tok.remaining(provider._clock())),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tok.expires_at)),
        "provider": provider.provider_name,
        "source": provider.source.name,
    }

    if artifact_path:
        try:
            status = read(provider, artifact_path)
        except (JFrogOidcError, urllib.error.URLError, OSError) as exc:
            result["read"] = {"path": artifact_path, "error": str(exc)}
            return result
        result["read"] = {"path": artifact_path, "status": status}
        result["ok"] = status == 200
    else:
        result["ok"] = True
    return result


def log_result(result: dict) -> None:
    """Print one JSON line about a selftest: what JFrog answered to this identity, in words.

    For the platform's own log (Cloud Logging, Container Apps console logs, Application Insights,
    CloudWatch), so the console can show the workload's live record of each exchange. Never a token:
    selftest returns none, and only its claims, check and read are used.
    """
    claims, check, read = result.get("claims") or {}, result.get("check") or {}, result.get("read") or {}
    status = check.get("status")
    if status is None and check.get("error"):
        m = re.search(r"HTTP (\d{3})", check["error"])
        status = int(m.group(1)) if m else None
    if check.get("username"):
        status = 200
        mins = int(check.get("expires_in") or 0) // 60
        msg = f"JFrog issued a token for {check['username']} via {check.get('provider')}, expires in {mins} min"
        if read.get("status") is not None:
            msg += f"; read {read.get('path')}: HTTP {read['status']}"
        elif read.get("error"):
            msg += f"; read {read.get('path')} failed: {read['error']}"
    else:
        msg = f"JFrog refused the exchange via {check.get('provider') or 'its provider'}: {check.get('error') or 'no answer'}"
    line = {"event": "jfrog_oidc_selftest", "ok": bool(result.get("ok")), "jfrog_status": status,
            "provider": check.get("provider"), "jfrog_user": check.get("username"),
            **{k: claims[k] for k in ("iss", "sub", "aud") if k in claims}, "message": msg[:600]}
    print(json.dumps(line), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jfrog_oidc.probe")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read", help="read an artifact and print the HTTP status").add_argument("path")
    sub.add_parser("selftest", help="claims + exchange + optional read").add_argument("path", nargs="?")
    args = parser.parse_args(argv)

    if args.command == "selftest":
        result = selftest(args.path)
        print(json.dumps({"event": "probe_selftest", **result}), flush=True)
        return 0 if result["ok"] else 1

    try:
        status = read(JFrogTokenProvider.from_env(), args.path)
    except (JFrogOidcError, ValueError, urllib.error.URLError, OSError) as exc:
        print(json.dumps({"event": "probe_read", "path": args.path, "error": str(exc)}), flush=True)
        return 1
    print(json.dumps({"event": "probe_read", "path": args.path, "status": status}), flush=True)
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
