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
) -> dict:
    """Check identity claims, the exchange and (optionally) an artifact read.

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

    try:
        tok = provider.token()
    except JFrogOidcError as exc:
        result["check"] = {"error": str(exc), "provider": provider.provider_name}
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
