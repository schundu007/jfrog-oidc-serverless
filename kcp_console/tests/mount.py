"""The KCP console is served only by the portal, at /k8s: tests mount it there."""

import io
import tempfile
from pathlib import Path

from console.server import ConsoleServer
from console.store import Store
from portal.server import K8S, SERVERLESS, Portal

TOKEN = "t" * 43


class Mounted:
    """A started portal serving kcp (and an empty Serverless section beside it)."""

    def __init__(self, kcp):
        self._tmp = tempfile.TemporaryDirectory()
        oidc = ConsoleServer(port=0, token=kcp.token, store=Store(Path(self._tmp.name)), base=SERVERLESS, log=io.StringIO())
        self.portal = Portal(oidc, kcp, port=0, log=kcp.log)
        self.portal.start()
        self.port = self.portal.port

    def stop(self):
        self.portal.stop()
        self._tmp.cleanup()


def path(p: str) -> str:
    return K8S + p
