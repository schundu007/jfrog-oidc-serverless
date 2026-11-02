"""python -m portal [--port 8760] [--no-browser]

One console for both: Serverless (Cloud Run, Container Apps, Function Apps) at /serverless and
Kubernetes (the kubelet credential provider) at /k8s, on one 127.0.0.1 port with one session
token (.console/session-token). Each section keeps its own store, runs and approval gates.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from console import deliver
from console.__main__ import EXPOSE_FLAG, REPO_ROOT, check_bind
from console.server import ConsoleServer
from console.store import Store
from kcp_console.jfrog import OperatorToken
from kcp_console.server import KcpServer
from kcp_console.store import Store as KcpStore, replay_store

from console.shell import Shell
from setup_console.ado import Ado
from setup_console.jobs import Jobs
from setup_console.server import SetupServer

from . import retention
from .connections import Connections
from .server import K8S, OIDC, SERVERLESS, SETUP, Portal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m portal")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    parser.add_argument("--new-token", action="store_true", help="sign every browser out: replace the saved token")
    parser.add_argument("--print-url", action="store_true",
                        help="print the sign-in URL for a portal already running on --port, then exit")
    parser.add_argument("--fixtures", metavar="PATH", help="Serverless: answer reads from recorded fixtures")
    parser.add_argument("--kcp-fixtures", metavar="DIR", help="Kubernetes: answer reads from recorded fixtures")
    parser.add_argument("--local-apply", action="store_true",
                        help="Serverless: plan, apply and wire from this machine instead of an ADO pull request")
    parser.add_argument(EXPOSE_FLAG, dest="exposed_ok", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.print_url:
        print(f"http://127.0.0.1:{args.port}/#t={Store().session_token()}")
        return 0
    refusal = check_bind(args.host, args.exposed_ok)
    if refusal:
        print(f"portal: {refusal}", file=sys.stderr)
        return 2

    store = Store()
    token = store.session_token(renew=args.new_token)
    # Settings: sign-ins made on the page (console-owned CLI config dirs, the in-memory JFrog token) win over
    # this machine's own logins; applied before any console makes its first call.
    connections = None if args.fixtures or args.kcp_fixtures else Connections(store.root)
    if connections:
        connections.apply()
    try:
        delivery = None
        if not (args.local_apply or args.fixtures):
            delivery = deliver.load_settings(REPO_ROOT / "pipelines" / "delivery.json",
                                             overrides=connections.file if connections else None)
        serverless = ConsoleServer(host=args.host, port=args.port, token=token, store=store, fixtures=args.fixtures,
                                   delivery=delivery, repo_root=REPO_ROOT, base=SERVERLESS)
        k8s = KcpServer(token=token, fixtures=args.kcp_fixtures, base=K8S,
                        store=replay_store(args.kcp_fixtures) if args.kcp_fixtures else KcpStore())
    except deliver.DeliveryError as exc:
        print(f"portal: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"portal: cannot load fixtures: {exc}", file=sys.stderr)
        return 2
    env_name = (delivery or {}).get("environment") or "test"
    operator = OperatorToken()
    # Stack changes run as ADO pipelines (the delivery repo's org and project); foundations and reads stay here.
    ado_client = None
    if delivery and not args.fixtures:
        ado_client = Ado(Shell(mode="live", log=lambda line: None), delivery["ado"]["organization"], delivery["ado"]["project"],
                         branch=delivery["ado"].get("target_branch") or "main", repo=delivery["ado"].get("repository") or "")
    setup = SetupServer(token=token, base=SETUP, env_name=env_name,
                        jobs=Jobs(env_name=env_name, token_source=operator.get, ado=ado_client))
    if connections:
        def retarget(ado):
            # Azure DevOps changed on Settings: the running consoles use it from the next call.
            if delivery is not None:
                delivery["ado"].update(ado)
            if ado_client:
                ado_client.retarget(ado["organization"], ado["project"], ado.get("target_branch") or "main",
                                    ado.get("repository") or "")
        connections.on_ado = retarget
    portal = Portal(serverless, k8s, host=args.host, port=args.port, setup_console=setup, connections=connections)
    try:
        portal.start()
    except OSError as exc:
        print(f"portal: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    setup.jobs.refresh_all()   # Infrastructure and JFrog OIDC show what is deployed without a Check click
    # Workloads and clusters whose resources are gone leave the live pages for the archive (kept 7 days).
    serverless.start_sweeper()
    k8s.start_sweeper()
    # Only the newest 7 runs are kept, live or archived, across every console: now and every minute.
    if not (args.fixtures or args.kcp_fixtures):
        def keep_seven():
            while True:
                retention.trim(serverless, k8s, setup.jobs)
                time.sleep(60)
        threading.Thread(target=keep_seven, name="run-retention", daemon=True).start()
    url = portal.url()
    print(f"JFrog OIDC console (Serverless {SERVERLESS}, Kubernetes {K8S}, Infrastructure {SETUP}, JFrog OIDC {OIDC}): {url}")
    print("Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        portal.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
