"""One in-process HTTP server for all emulators, routed by path prefix.

Each emulator implements ``handle(req) -> (status, body, headers)`` where body
is a dict (sent as JSON), str or bytes. Requests are serialised with one lock:
the engine makes one call at a time, and the lock keeps emulator state simple.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Request:
    method: str
    path: str            # path after the emulator's prefix
    query: dict
    headers: dict        # lower-cased names
    body: bytes = b""
    raw_query: str = ""

    def json(self) -> dict:
        try:
            return json.loads(self.body or b"{}")
        except ValueError:
            return {}

    def bearer(self) -> str:
        auth = self.headers.get("authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


@dataclass
class Stats:
    requests: int = 0
    by_prefix: dict = field(default_factory=dict)


class EmulatorServer:
    def __init__(self, routes: dict):
        # Longest prefix first so /gcp/ar does not shadow a more specific route.
        self.routes = sorted(routes.items(), key=lambda kv: -len(kv[0]))
        self.lock = threading.Lock()
        self.stats = Stats()
        self._httpd = None
        self._thread = None

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _dispatch(self):
                url = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                for prefix, emu in outer.routes:
                    if url.path == prefix or url.path.startswith(prefix + "/"):
                        req = Request(
                            method=self.command,
                            path=url.path[len(prefix):] or "/",
                            query=dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True)),
                            headers={k.lower(): v for k, v in self.headers.items()},
                            body=body,
                            raw_query=url.query,
                        )
                        with outer.lock:
                            outer.stats.requests += 1
                            outer.stats.by_prefix[prefix] = outer.stats.by_prefix.get(prefix, 0) + 1
                            try:
                                status, payload, headers = emu.handle(req)
                            except Exception as exc:  # noqa: BLE001 - an emulator bug must not hang the client
                                status, payload, headers = 500, {"error": f"emulator error: {type(exc).__name__}: {exc}"}, {}
                        return self._send(status, payload, headers)
                self._send(404, {"error": f"no emulator for {url.path}"}, {})

            def _send(self, status, payload, headers):
                if isinstance(payload, (dict, list)):
                    data, ctype = json.dumps(payload).encode(), "application/json"
                elif isinstance(payload, str):
                    data, ctype = payload.encode(), "text/plain"
                else:
                    data, ctype = payload or b"", "application/octet-stream"
                self.send_response(status)
                self.send_header("Content-Type", headers.pop("Content-Type", ctype) if headers else ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02},
                                        name="sim-emulators", daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        self.host = f"127.0.0.1:{port}"
        self.base = f"http://{self.host}"
        return self.base

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
