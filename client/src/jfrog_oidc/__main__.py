"""CLI for scripts and entrypoints.

    python -m jfrog_oidc claims         # show the platform token's claims (debug mapping mismatches)
    python -m jfrog_oidc check          # exchange and print username/expiry (no token)
    python -m jfrog_oidc token          # print the access token (for piping only)
    python -m jfrog_oidc docker-login   # docker login <host> --password-stdin
    python -m jfrog_oidc pip --repo pypi-remote -- download ...
                                        # run pip against a JFrog PyPI repo; the token goes to
                                        # pip through PIP_INDEX_URL in its environment, never argv
    python -m jfrog_oidc mvn --repo maven-remote -- -B package
                                        # run Maven with JFrog as the only mirror
    python -m jfrog_oidc dotnet --repo nuget-remote -- restore <project>
                                        # restore NuGet packages from JFrog only
                                        # (mvn/dotnet: a temp config names the repo; the token is
                                        # only in the tool's environment, never argv or a file)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

from . import JFrogOidcError, JFrogTokenProvider, detect_source
from .exchange import _jwt_claims


# Build tools that resolve packages through JFrog, and the env var naming each one's default repo.
TOOLS = {"pip": "JFROG_PYPI_REPO", "mvn": "JFROG_MAVEN_REPO", "dotnet": "JFROG_NUGET_REPO"}

_MAVEN_SETTINGS = """<settings>
  <mirrors><mirror><id>jfrog</id><mirrorOf>*</mirrorOf><url>{url}</url></mirror></mirrors>
  <servers><server><id>jfrog</id>
    <username>${{env.JFROG_OIDC_USER}}</username><password>${{env.JFROG_OIDC_TOKEN}}</password>
  </server></servers>
</settings>
"""

_NUGET_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="jfrog" value="{url}" />
  </packageSources>
</configuration>
"""


def _build_tool(tool, repo, tool_args, jfrog_url, tok) -> int:
    """Run mvn or dotnet restore with JFrog as the only package source."""
    from xml.sax.saxutils import escape
    repo_q = urllib.parse.quote(repo, safe="")
    with tempfile.TemporaryDirectory(prefix="jfrog-oidc-") as tmp:
        if tool == "mvn":
            cfg = os.path.join(tmp, "settings.xml")
            text = _MAVEN_SETTINGS.format(url=escape(f"{jfrog_url}/artifactory/{repo_q}"))
            cmd = ["mvn", "-s", cfg, *tool_args]
            env = dict(os.environ, JFROG_OIDC_USER=tok.username, JFROG_OIDC_TOKEN=tok.access_token)
        else:
            cfg = os.path.join(tmp, "nuget.config")
            url = f"{jfrog_url}/artifactory/api/nuget/v3/{repo_q}/index.json"
            text = _NUGET_CONFIG.format(url=escape(url, {'"': "&quot;"}))
            cmd = ["dotnet", *tool_args, "--configfile", cfg]
            env = dict(os.environ, NuGetPackageSourceCredentials_jfrog=(
                f"Username={tok.username};Password={tok.access_token}"))
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(text)
        return subprocess.run(cmd, env=env).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jfrog_oidc")
    parser.add_argument("command", choices=["claims", "check", "token", "docker-login", *TOOLS])
    parser.add_argument("--registry", help="registry host for docker-login (default: JFROG_URL host)")
    parser.add_argument("--repo", default="",
                        help="JFrog repository key for pip/mvn/dotnet "
                             "(default: JFROG_PYPI_REPO, JFROG_MAVEN_REPO, JFROG_NUGET_REPO)")
    parser.add_argument("-v", "--verbose", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    pip_args = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if args.command in TOOLS:
        args.repo = args.repo or os.environ.get(TOOLS[args.command], "")
        if not args.repo or not pip_args:
            parser.error(f"{args.command} needs --repo (or {TOOLS[args.command]}) and arguments after --")
        if args.command == "dotnet" and pip_args[0] != "restore":
            parser.error("dotnet runs restore only; build and publish with --no-restore afterwards")
    elif pip_args:
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
    if args.command in ("mvn", "dotnet"):
        return _build_tool(args.command, args.repo, pip_args, provider.jfrog_url.rstrip("/"), tok)

    registry = args.registry or urllib.parse.urlsplit(provider.jfrog_url).hostname
    proc = subprocess.run(
        ["docker", "login", registry, "--username", tok.username, "--password-stdin"],
        input=tok.access_token.encode(),
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
