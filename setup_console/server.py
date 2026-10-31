"""Setup section of the portal (/setup): deploy and destroy test infrastructure.

Mounted by the portal like the other two sections, with the same session token, Host/Origin
checks, CSP and redaction. Nothing runs without a plan, an approval of that plan's sha256 and,
for anything that deletes, the stack name typed.
"""

from __future__ import annotations

import hmac
import json
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from console import redact
from console.server import SECURITY_HEADERS

from . import stacks
from .jobs import JobError, Jobs

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
PAGE_ROUTES = re.compile(r"^/(?:[a-z0-9-]+/?)?$")
MAX_BODY = 128 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class SetupServer:
    def __init__(self, token: str, jobs: Jobs | None = None, env_name: str = "test", base: str = "/setup",
                 host: str = "127.0.0.1", port: int = 0, log=sys.stderr, root=stacks.STACKS_DIR):
        self.token, self.base, self.host, self.port, self.log = token, base, host, port, log
        self.env_name, self.root = env_name, root
        self.jobs = jobs or Jobs(env_name=env_name, root=root)
        self.static_files = {p.name for p in STATIC_DIR.iterdir() if p.is_file() and p.suffix in CONTENT_TYPES}

    def allowed_hosts(self) -> set:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


def make_handler(console: SetupServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "setup-console"
        sys_version = ""

        def log_message(self, fmt, *args):
            console.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {fmt % args}") + "\n")

        def _headers(self, status, ctype, length):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(length))
            self.end_headers()

        def _json(self, status, body):
            data = json.dumps(redact.obj(body)).encode()
            self._headers(status, "application/json", len(data))
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                raise HttpError(413, "request body too large")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise HttpError(415, "Content-Type must be application/json")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise HttpError(400, "invalid JSON") from None
            if not isinstance(body, dict):
                raise HttpError(400, "JSON body must be an object")
            return body

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                site = self.headers.get("Sec-Fetch-Site", "same-origin")
                return site in ("same-origin", "none") or (
                    self.command == "GET" and self.headers.get("Sec-Fetch-Mode") == "navigate" and not self.path.startswith("/api/"))
            return origin in {f"http://{h}" for h in console.allowed_hosts()}

        def _token_ok(self) -> bool:
            given = self.headers.get("X-Console-Token", "")
            return bool(given) and hmac.compare_digest(given.encode(), console.token.encode())

        def _dispatch(self):
            try:
                if self.headers.get("Host", "") not in console.allowed_hosts():
                    raise HttpError(403, "bad Host header")
                if not self._origin_ok():
                    raise HttpError(403, "cross-origin request refused")
                path = urllib.parse.urlsplit(self.path).path
                if path.startswith("/api/"):
                    if not self._token_ok():
                        raise HttpError(401, "missing or invalid X-Console-Token")
                    return self._api(self.command, path.strip("/").split("/")[1:])
                if self.command != "GET":
                    raise HttpError(405, "method not allowed")
                return self._static(path)
            except HttpError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (JobError,) as exc:
                self._json(exc.status, {"error": str(exc)})
            except stacks.StackError as exc:
                self._json(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _dispatch

        def _static(self, path):
            name = "index.html" if PAGE_ROUTES.match(path) else path[len("/static/"):] if path.startswith("/static/") else None
            if name not in console.static_files:
                raise HttpError(404, "not found")
            data = (STATIC_DIR / name).read_bytes()
            if name == "index.html":
                if self.headers.get("Sec-Fetch-Site") in ("none", "same-origin"):
                    data = data.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n  <meta name="console-token" content="'
                                        + console.token.encode() + b'">', 1)
                base = console.base.encode()
                data = data.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n  <meta name="console-base" content="'
                                    + base + b'">', 1).replace(b'"/static/', b'"' + base + b'/static/')
            self._headers(200, CONTENT_TYPES[Path(name).suffix], len(data))
            self.wfile.write(data)

        def _api(self, method, parts):
            m = (method, parts[0] if parts else "", len(parts))
            if m == ("GET", "stacks", 1):
                return self._json(200, {"env": console.env_name, "stacks": console.jobs.stacks()})
            if parts[:1] == ["stacks"] and len(parts) == 3:
                sid, what = parts[1], parts[2]
                stacks.get(sid)
                if what == "vars" and method == "GET":
                    return self._json(200, {"text": stacks.read_vars(sid, console.env_name, console.root),
                                            "file": str(stacks.vars_file(sid, console.env_name, console.root).relative_to(console.root))})
                if what == "vars" and method == "PUT":
                    stacks.write_vars(sid, console.env_name, self._body().get("text"), console.root)
                    return self._json(200, {"saved": True})
                if what == "jobs" and method == "POST":
                    b = self._body()
                    return self._json(202, console.jobs.start(sid, b.get("action"), b.get("approval")))
                if what == "plan" and method == "DELETE":
                    console.jobs.discard(sid)
                    return self._json(200, {"discarded": True})
            if m == ("GET", "jobs", 2):
                return self._json(200, console.jobs.get(parts[1]))
            raise HttpError(404, "not found")

    return Handler
