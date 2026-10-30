"""One origin for both consoles: Serverless (console/) at /serverless, Kubernetes (kcp_console/) at /k8s.

The portal holds no product logic. It removes the section prefix and hands the request to that
console's own handler, which keeps its own Host/Origin/token checks, CSP and redaction. The two
consoles share only the session token; their stores, runs and approval gates stay separate.
Old standalone paths (/gcp, /azure, /clusters, /configure) redirect into their section.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from console import redact
from console import server as oidc
from kcp_console import server as kcp

SERVERLESS, K8S = "/serverless", "/k8s"
STATIC_DIR = Path(__file__).parent / "static"
# Home is a read-only dashboard over both sections; every other page belongs to one section.
STATIC = {"/": "index.html", "/dashboard.js": "dashboard.js", "/dashboard.css": "dashboard.css"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def section_of(path: str):
    """(section, path inside it) for a portal path, or (None, path) for the portal's own pages."""
    for prefix in (SERVERLESS, K8S):
        if path == prefix or path.startswith(prefix + "/"):
            return prefix, path[len(prefix):] or "/"
    return None, path


def legacy_target(path: str):
    """Where an old standalone URL lives now, or None."""
    first = path.split("/")[1] if path.startswith("/") else ""
    if first in ("gcp", "azure"):
        return SERVERLESS + path
    if first in ("clusters", "configure"):
        return K8S + path
    return None


class Portal:
    def __init__(self, oidc_console, kcp_console, host: str = "127.0.0.1", port: int = 8760, log=sys.stderr):
        if oidc_console.token != kcp_console.token:
            raise ValueError("both consoles must share the portal's session token")
        self.oidc, self.kcp = oidc_console, kcp_console
        self.host, self.requested_port, self.log = host, port, log
        self.token = oidc_console.token
        self.port = None
        self._httpd = None

    def allowed_hosts(self) -> set:
        return self.oidc.allowed_hosts()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.requested_port), _make_handler(self))
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        # The consoles are mounted, not started: give them what their own start() would set,
        # so their Host/Origin checks expect the portal's address.
        for c, mod in ((self.oidc, oidc), (self.kcp, kcp)):
            c.host, c.port = self.host, self.port
            c.static_files = {p.name for p in mod.STATIC_DIR.iterdir() if p.is_file() and p.suffix in mod.CONTENT_TYPES}
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                         name="portal-http", daemon=True).start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.oidc.stop()
        self.kcp.stop()


def _make_handler(portal: Portal):
    handlers = {SERVERLESS: oidc._make_handler(portal.oidc), K8S: kcp.make_handler(portal.kcp)}

    class Handler(BaseHTTPRequestHandler):
        server_version = "jfrog-oidc-portal"
        sys_version = ""

        def log_message(self, fmt, *args):
            portal.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {fmt % args}") + "\n")

        def _send(self, status: int, ctype: str, data: bytes = b"", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in oidc.SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self):
            split = urllib.parse.urlsplit(self.path)
            section, inner = section_of(split.path)
            if section:
                # Hand the request to that console's handler as if it were standalone. The class is
                # switched back afterwards: a keep-alive connection's next request must come here again.
                self.path = urllib.parse.urlunsplit(("", "", inner, split.query, ""))
                target = handlers[section]
                self.__class__ = target
                try:
                    return target._dispatch(self)
                finally:
                    self.__class__ = Handler
            try:
                if self.headers.get("Host", "") not in portal.allowed_hosts():
                    return self._send(403, "text/plain; charset=utf-8", b"bad Host header")
                if split.path == "/favicon.ico":
                    return self._send(204, "text/plain")
                to = legacy_target(split.path)
                if to:
                    return self._send(308, "text/plain; charset=utf-8", b"",
                                      {"Location": urllib.parse.urlunsplit(("", "", to, split.query, ""))})
                name = STATIC.get(split.path)
                if self.command != "GET" or not name:
                    return self._send(404, "text/plain; charset=utf-8", b"not found")
                data = (STATIC_DIR / name).read_bytes()
                # Like the consoles: the page carries the token only when the operator opened it directly.
                if name == "index.html" and self.headers.get("Sec-Fetch-Site") in ("none", "same-origin"):
                    data = data.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n  <meta name="console-token" content="'
                                        + portal.token.encode() + b'">', 1)
                self._send(200, CONTENT_TYPES[Path(name).suffix], data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _dispatch

    return Handler
