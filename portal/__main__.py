"""python -m portal [--port 8760] [--no-browser]

One console: Serverless (Web Apps, Container Apps, Function Apps, Cloud Run, App Engine) at /serverless,
Infrastructure at /setup and JFrog OIDC at /oidc, on one 127.0.0.1 port with one session token
(.console/session-token).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from console import deliver, jfrog_operator
from console.__main__ import EXPOSE_FLAG, REPO_ROOT, check_bind
from console.server import ConsoleServer
from console.store import Store

from console.shell import Shell
from setup_console.ado import Ado
from setup_console.jobs import Jobs
from setup_console.server import SetupServer

from . import chain, retention
from .connections import Connections
from .server import OIDC, SERVERLESS, SETUP, Portal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m portal")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    parser.add_argument("--new-token", action="store_true", help="sign every browser out: replace the saved token")
    parser.add_argument("--print-url", action="store_true",
                        help="print the sign-in URL for a portal already running on --port, then exit")
    parser.add_argument("--fixtures", metavar="PATH", help="Serverless: answer reads from recorded fixtures")
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
    connections = None if args.fixtures else Connections(store.root)
    if connections:
        connections.apply()
    try:
        delivery = None
        if not (args.local_apply or args.fixtures):
            delivery = deliver.load_settings(REPO_ROOT / "pipelines" / "delivery.json",
                                             overrides=connections.file if connections else None)
        serverless = ConsoleServer(host=args.host, port=args.port, token=token, store=store, fixtures=args.fixtures,
                                   delivery=delivery, repo_root=REPO_ROOT, base=SERVERLESS)
    except deliver.DeliveryError as exc:
        print(f"portal: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"portal: cannot load fixtures: {exc}", file=sys.stderr)
        return 2
    env_name = (delivery or {}).get("environment") or "test"
    # Stack changes run as ADO pipelines (the delivery repo's org and project); pipeline identity stacks and reads stay here.
    ado_client = None
    if delivery and not args.fixtures:
        ado_client = Ado(Shell(mode="live", log=lambda line: None), delivery["ado"]["organization"], delivery["ado"]["project"],
                         branch=delivery["ado"].get("target_branch") or "main", repo=delivery["ado"].get("repository") or "")
    setup = SetupServer(token=token, base=SETUP, env_name=env_name,
                        jobs=Jobs(env_name=env_name, token_source=jfrog_operator.Tokens().get, ado=ado_client))
    if connections:
        def retarget(ado):
            # Azure DevOps changed on Settings: the running consoles use it from the next call. A console
            # started without Azure DevOps (a new environment, nothing in delivery.json) switches to
            # delivery mode here, without a restart.
            nonlocal delivery, ado_client
            if delivery is None:
                try:
                    delivery = deliver.load_settings(REPO_ROOT / "pipelines" / "delivery.json", overrides=connections.file)
                except deliver.DeliveryError:
                    delivery = None
                if delivery is None:
                    return
                serverless.delivery = serverless.runs.delivery = delivery
                ado_client = Ado(Shell(mode="live", log=lambda line: None), ado["organization"], ado["project"],
                                 branch=ado.get("target_branch") or "main", repo=ado.get("repository") or "")
                setup.jobs.ado = ado_client
                setup.jobs.onboard = chain.onboarder(serverless)
                return
            delivery["ado"].update(ado)
            if ado_client:
                ado_client.retarget(ado["organization"], ado["project"], ado.get("target_branch") or "main",
                                    ado.get("repository") or "")
        connections.on_ado = retarget
    if delivery and not args.fixtures:   # Deploy on Infrastructure: build and deploy, then onboard
        setup.jobs.onboard = chain.onboarder(serverless)
    portal = Portal(serverless, host=args.host, port=args.port, setup_console=setup, connections=connections)
    try:
        portal.start()
    except OSError as exc:
        print(f"portal: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    setup.jobs.refresh_all()   # Infrastructure and JFrog OIDC show what is deployed without a Check click
    # Workloads whose resources are gone leave the live pages for the archive (kept 7 days).
    serverless.start_sweeper()
    # Only the newest 7 runs are kept, live or archived, across every console: now and every minute.
    if not args.fixtures:
        def keep_seven():
            while True:
                retention.trim(serverless, setup.jobs)
                time.sleep(60)
        threading.Thread(target=keep_seven, name="run-retention", daemon=True).start()
    url = portal.url()
    print(f"JFrog OIDC console (Serverless {SERVERLESS}, Infrastructure {SETUP}, JFrog OIDC {OIDC}): {url}")
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
