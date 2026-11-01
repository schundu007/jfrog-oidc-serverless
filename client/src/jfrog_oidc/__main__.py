"""CLI for scripts and entrypoints.

    python -m jfrog_oidc claims         # show the platform token's claims (debug mapping mismatches)
    python -m jfrog_oidc check          # exchange and print username/expiry (no token)
    python -m jfrog_oidc token          # print the access token (for piping only)
    python -m jfrog_oidc docker-login   # docker login <host> --password-stdin
    python -m jfrog_oidc pip --repo pypi-remote -- download ...
                                        # run pip against a JFrog PyPI repo; the token goes to
                                        # pip through PIP_INDEX_URL in its environment, never argv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse

from . import JFrogOidcError, JFrogTokenProvider, detect_source
from .exchange import _jwt_claims


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jfrog_oidc")
    parser.add_argument("command", choices=["claims", "check", "token", "docker-login", "pip"])
    parser.add_argument("--registry", help="registry host for docker-login (default: JFROG_URL host)")
    parser.add_argument("--repo", default=os.environ.get("JFROG_PYPI_REPO", ""),
                        help="JFrog PyPI repository key for pip (default: JFROG_PYPI_REPO)")
    parser.add_argument("-v", "--verbose", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    pip_args = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if args.command == "pip" and (not args.repo or not pip_args):
        parser.error("pip needs --repo (or JFROG_PYPI_REPO) and pip arguments after --")
    if args.command != "pip" and pip_args:
        parser.error(f"unexpected arguments: {' '.join(pip_args)}")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, stream=sys.stderr)

    if args.command == "claims":
        try:
            source = detect_source()
            audience = os.environ.get("JFROG_OIDC_AUDIENCE", "")
            if not audience:
                raise ValueError("JFROG_OIDC_AUDIENCE is not set")
            claims = _jwt_claims(source.get_token(audience))
        except (JFrogOidcError, ValueError) as exc:
            print(f"jfrog_oidc: {exc}", file=sys.stderr)
            return 1
        keep = ("iss", "aud", "sub", "oid", "tid", "azp", "email", "ver", "iat", "exp")
        print(json.dumps({k: claims[k] for k in keep if k in claims}, indent=2))
        print("Compare iss/aud and sub (GCP) or oid/tid (Azure) with the JFrog integration and claims JSON.", file=sys.stderr)
        return 0

    try:
        provider = JFrogTokenProvider.from_env()
        tok = provider.token()
    except (JFrogOidcError, ValueError) as exc:
        print(f"jfrog_oidc: {exc}", file=sys.stderr)
        return 1

    if args.command == "check":
        print(json.dumps({
            "username": tok.username,
            "expires_in": int(tok.remaining()),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tok.expires_at)),
            "provider": provider.provider_name,
            "source": provider.source.name,
        }))
        return 0
    if args.command == "token":
        if sys.stdout.isatty():
            print("jfrog_oidc: refusing to print a token to a terminal; pipe it instead", file=sys.stderr)
            return 2
        sys.stdout.write(tok.access_token)
        return 0

    if args.command == "pip":
        quote = lambda v: urllib.parse.quote(v, safe="")  # noqa: E731
        parts = urllib.parse.urlsplit(provider.jfrog_url)
        index = (f"{parts.scheme}://{quote(tok.username)}:{quote(tok.access_token)}@{parts.netloc}"
                 f"/artifactory/api/pypi/{quote(args.repo)}/simple")
        env = dict(os.environ, PIP_INDEX_URL=index, PIP_EXTRA_INDEX_URL="", PIP_NO_INPUT="1")
        return subprocess.run([sys.executable, "-m", "pip", *pip_args], env=env).returncode

    registry = args.registry or urllib.parse.urlsplit(provider.jfrog_url).hostname
    proc = subprocess.run(
        ["docker", "login", registry, "--username", tok.username, "--password-stdin"],
        input=tok.access_token.encode(),
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
