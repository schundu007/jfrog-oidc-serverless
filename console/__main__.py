"""python -m console [--port 8765] [--no-browser]

Starts the local console on 127.0.0.1 and prints a URL whose fragment carries
the session token. The token is kept in .console/session-token (0600) so a
restart doesn't sign the browser out; --new-token replaces it. The fragment never leaves the browser; treat the URL like a
password while the console runs.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
import threading
import webbrowser

from .server import ConsoleServer
from .store import Store

EXPOSE_FLAG = "--i-know-this-exposes-credentials"


def check_bind(host: str, exposed_ok: bool):
    """None if binding to host is allowed, else the reason it is refused."""
    if host == "localhost":
        return None
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback or exposed_ok:
        return None
    return (f"refusing to bind to {host}: the console acts with your gcloud/az logins and JFROG_ACCESS_TOKEN. "
            f"Use 127.0.0.1, or pass {EXPOSE_FLAG} if you really mean it.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m console")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    parser.add_argument("--new-token", action="store_true",
                        help="sign every browser out: replace the saved session token")
    parser.add_argument("--print-url", action="store_true",
                        help="print the sign-in URL for a console already running on --port, then exit")
    parser.add_argument(EXPOSE_FLAG, dest="exposed_ok", action="store_true", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fixtures", metavar="PATH",
                      help="answer Preflight/Discover from recorded fixtures (a .json file or a directory); nothing runs")
    mode.add_argument("--record", metavar="DIR",
                      help="run Preflight/Discover for real and save redacted copies of every call to DIR")
    args = parser.parse_args(argv)

    if args.print_url:
        # The token survives restarts (.console/session-token), so this is the link for the running console.
        print(f"http://127.0.0.1:{args.port}/#t={Store().session_token()}")
        return 0

    refusal = check_bind(args.host, args.exposed_ok)
    if refusal:
        print(f"console: {refusal}", file=sys.stderr)
        return 2

    try:
        store = Store()
        console = ConsoleServer(host=args.host, port=args.port, token=store.session_token(renew=args.new_token),
                                store=store, fixtures=args.fixtures, record_dir=args.record)
    except (OSError, ValueError) as exc:
        print(f"console: cannot load fixtures: {exc}", file=sys.stderr)
        return 2
    try:
        console.start()
    except OSError as exc:
        print(f"console: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    url = console.url()
    print(f"JFrog OIDC console: {url}")
    print("Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        console.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
