"""Cloud Run service: read a JFrog artifact with a short-lived OIDC token (stdlib only).

The service's Google identity token is exchanged for a JFrog access token
in-process, once per token lifetime. Env: the client's (JFROG_URL,
JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE, JFROG_OIDC_SOURCE), set by the
console's Wire step, plus optional JFROG_HEALTHCHECK_PATH.

GET /healthz/jfrog       claims, exchange and read status (never the token)
GET /?path=<repo/folder> files in that folder; &fetch=1 downloads the first one
Deploy without --allow-unauthenticated so Cloud Run IAM protects it.
"""

import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from jfrog_oidc import JFrogOidcError, JFrogTokenProvider, download, get_json
from jfrog_oidc.probe import selftest

_jfrog = None


def jfrog() -> JFrogTokenProvider:
    global _jfrog
    if _jfrog is None:
        _jfrog = JFrogTokenProvider.from_env()
    return _jfrog


def handle(path: str, query: dict) -> tuple[int, dict]:
    if path == "/healthz/jfrog":
        result = selftest(os.environ.get("JFROG_HEALTHCHECK_PATH") or None, provider=jfrog())
        return (200 if result["ok"] else 503), result
    folder = query.get("path", ["generic-local/models"])[0]
    listing = get_json(jfrog(), f"/artifactory/api/storage/{folder}")
    files = [c["uri"].lstrip("/") for c in listing.get("children", []) if not c.get("folder")]
    if query.get("fetch") and files:
        dest = os.path.join(tempfile.gettempdir(), files[0])
        download(jfrog(), f"{folder}/{files[0]}", dest)
        return 200, {"downloaded": files[0], "bytes": os.path.getsize(dest)}
    return 200, {"files": files, "token_user": jfrog().token().username}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        try:
            status, body = handle(url.path, parse_qs(url.query))
        except (JFrogOidcError, ValueError) as exc:
            status, body = 502, {"error": str(exc)}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"{self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}")


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
