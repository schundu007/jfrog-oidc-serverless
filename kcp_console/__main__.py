"""python3 -m kcp_console [--port 8766] [--no-browser] [--fixtures DIR]

Starts the KCP console on 127.0.0.1 and prints a URL whose fragment carries the
session token. The token is kept in .kcp-console/session-token (0600) so a
restart doesn't sign the browser out; --new-token replaces it. Treat the URL
like a password while the console runs.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
import threading
import webbrowser

from .server import KcpServer
from .store import RunStore, Store


def replay_store(fixtures: str, root: Path | None = None) -> Store:
    """Replay mode keeps its own store (.kcp-console/replay), seeded once from the fixtures'
    *.seed.json files: the saved config and the recorded run history."""
    store = Store(Path(root) if root else Store().root / "replay")
    fx = Path(fixtures)
    cfg_file, hist_file = fx / "config.seed.json", fx / "history.seed.json"
    if cfg_file.is_file() and not store.list():
        cfg = json.loads(cfg_file.read_text())
        store.put(cfg["name"], cfg)
    runs = RunStore(store.root)
    if hist_file.is_file() and not runs.all():
        for item in json.loads(hist_file.read_text()):
            runs.save(item["run"])
            for ev in item["run"].get("events", []):
                runs.event(item["run"]["id"], ev)
            if item.get("log"):
                runs.save_log(item["run"]["id"], item["log"])
    return store

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
    return (f"refusing to bind to {host}: the console acts with your az login, ADO access and JFrog tokens it mints. "
            f"Use 127.0.0.1, or pass {EXPOSE_FLAG} if you really mean it.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m kcp_console")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    parser.add_argument("--new-token", action="store_true", help="sign every browser out: replace the saved token")
    parser.add_argument("--print-url", action="store_true",
                        help="print the sign-in URL for a console already running on --port, then exit")
    parser.add_argument("--fixtures", metavar="DIR", help="answer every read from recorded fixtures; nothing runs")
    parser.add_argument(EXPOSE_FLAG, dest="exposed_ok", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.print_url:
        print(f"http://127.0.0.1:{args.port}/#t={Store().session_token()}")
        return 0
    refusal = check_bind(args.host, args.exposed_ok)
    if refusal:
        print(f"kcp_console: {refusal}", file=sys.stderr)
        return 2
    token = Store().session_token(renew=args.new_token)
    try:
        store = replay_store(args.fixtures) if args.fixtures else Store()
        console = KcpServer(host=args.host, port=args.port, token=token, store=store, fixtures=args.fixtures)
    except (OSError, ValueError) as exc:
        print(f"kcp_console: cannot load fixtures: {exc}", file=sys.stderr)
        return 2
    try:
        console.start()
    except OSError as exc:
        print(f"kcp_console: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    url = console.url()
    print(f"KCP console: {url}")
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
