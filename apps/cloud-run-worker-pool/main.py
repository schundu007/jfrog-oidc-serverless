"""Cloud Run worker pool: an always-on worker (no HTTP endpoint) that reads JFrog with a short-lived
OIDC token (stdlib only).

The worker's Google identity token is exchanged for a JFrog access token in-process; the client
caches it and exchanges again before it expires. Every JFROG_POLL_SECONDS (default 60) the worker
checks its claims, a fresh exchange and, with JFROG_HEALTHCHECK_PATH (<repo>/<path>), an artifact read,
and logs one JSON line with the result. The token itself is never printed.

Env: the client's (JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE, JFROG_OIDC_SOURCE), set by
the console's Wire step.
"""

import json
import os
import signal
import sys
import threading

from jfrog_oidc import JFrogOidcError, JFrogTokenProvider
from jfrog_oidc.probe import selftest

stop = threading.Event()


def check(provider) -> dict:
    result = selftest(os.environ.get("JFROG_HEALTHCHECK_PATH") or None, provider=provider, fresh=True)
    print(json.dumps({"event": "jfrog_check", "ok": result["ok"], "claims": result.get("claims"),
                      "check": result.get("check"), "read": result.get("read")}), flush=True)
    return result


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    every = max(10, int(os.environ.get("JFROG_POLL_SECONDS", "60")))
    try:
        provider = JFrogTokenProvider.from_env()
    except (JFrogOidcError, ValueError) as exc:
        # Not wired yet (no JFROG_* settings): keep running so the pool stays up, and say why.
        print(json.dumps({"event": "jfrog_check", "ok": False, "error": str(exc)}), flush=True)
        provider = None
    while not stop.is_set():
        if provider is None:
            try:
                provider = JFrogTokenProvider.from_env()
            except (JFrogOidcError, ValueError):
                provider = None
        if provider is not None:
            check(provider)
        stop.wait(every)
    print(json.dumps({"event": "stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
