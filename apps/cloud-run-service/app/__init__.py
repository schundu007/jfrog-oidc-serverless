"""JFrog browser: a web app and JSON API that read JFrog with the service's own
cloud identity, exchanged for a short-lived JFrog token (no stored secret).

Pages        /                          identity, repositories, home folder
             /browse/<repo>/<path>      a folder's items or a file's details
             /download/<repo>/<path>    the file, streamed from JFrog
API (JSON)   /api/v1/identity, /api/v1/repositories, /api/v1/items/<repo>/<path>
Health       /healthz (process up), /readyz (JFrog sign-in works),
             /healthz/jfrog (claims, exchange and a read: what the console's Verify calls)

Run: gunicorn --config gunicorn.conf.py "app:create_app()"
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, stream_with_context, url_for
from werkzeug.exceptions import HTTPException

from jfrog_oidc.probe import log_result, selftest

from . import logs, paths
from .artifactory import Artifactory, Clock, JFrogFailure
from .config import Settings

log = logging.getLogger("app")

CSP = ("default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
QUIET = {"/healthz", "/readyz"}   # probes: not request-logged


def _human(size: Optional[int]) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def create_app(settings: Optional[Settings] = None, artifactory: Optional[Artifactory] = None,
               clock: Optional[Clock] = None) -> Flask:
    settings = settings or Settings.from_env()
    jfrog = artifactory or Artifactory()
    clock = clock or Clock()
    if os.environ.get("APP_JSON_LOGS", "1") == "1":
        logs.setup(os.environ.get("LOG_LEVEL", "INFO"))

    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=64 * 1024, SEND_FILE_MAX_AGE_DEFAULT=3600)
    app.json.sort_keys = False
    app.jinja_env.filters["human"] = _human
    app.extensions["jfrog"] = jfrog

    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.path.startswith("/healthz") or \
            request.accept_mimetypes.best_match(["text/html", "application/json"]) == "application/json"

    # -- request lifecycle ---------------------------------------------------------------------

    @app.before_request
    def _start():
        g.started = time.perf_counter()
        trace_id, g.trace_name = logs.trace(request.headers.get(logs.TRACE_HEADER), logs.gcp_project())
        g.request_id = trace_id or uuid.uuid4().hex

    @app.after_request
    def _finish(resp: Response):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Request-Id"] = g.get("request_id", "")
        if request.path not in QUIET:
            latency = time.perf_counter() - g.get("started", time.perf_counter())
            log.info("%s %s %s", request.method, request.path, resp.status_code, extra={
                "httpRequest": {"requestMethod": request.method, "requestUrl": request.path,
                                "status": resp.status_code, "userAgent": request.user_agent.string[:200],
                                "remoteIp": request.remote_addr, "latency": f"{latency:.3f}s"},
                "trace_name": g.get("trace_name"), "request_id": g.get("request_id")})
        return resp

    @app.errorhandler(paths.BadPath)
    def _bad_path(exc):
        return _error(400, str(exc))

    @app.errorhandler(JFrogFailure)
    def _jfrog_failure(exc: JFrogFailure):
        if exc.status >= 500:
            log.warning("JFrog: %s", exc, extra={"request_id": g.get("request_id")})
        return _error(exc.status, str(exc))

    @app.errorhandler(HTTPException)
    def _http(exc: HTTPException):
        return _error(exc.code or 500, exc.description or exc.name)

    @app.errorhandler(Exception)
    def _unexpected(exc: Exception):
        log.exception("unhandled error", extra={"request_id": g.get("request_id")})
        return _error(500, "internal error")

    def _error(status: int, message: str):
        if wants_json():
            return jsonify(error=message, request_id=g.get("request_id")), status
        return render_template("error.html", status=status, message=message, settings=settings,
                               request_id=g.get("request_id")), status

    # -- health ----------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", version=settings.version, uptime_seconds=clock.uptime())

    @app.get("/readyz")
    def readyz():
        try:
            jfrog.ready()
        except JFrogFailure as exc:
            return jsonify(status="unavailable", reason=str(exc)), 503
        return jsonify(status="ready")

    @app.get("/healthz/jfrog")
    def healthz_jfrog():
        try:
            provider = jfrog.provider
        except JFrogFailure as exc:
            return jsonify(ok=False, check={"error": str(exc)}), 503
        result = selftest(settings.healthcheck_path, provider=provider, fresh=request.args.get("fresh") == "1")
        log_result(result)   # what JFrog answered, never a token
        return jsonify(result), (200 if result["ok"] else 503)

    # -- pages -----------------------------------------------------------------------------------

    @app.get("/")
    def home():
        identity, repos, problem = None, [], None
        try:
            identity = jfrog.identity()
            repos = jfrog.repositories()
        except JFrogFailure as exc:
            problem = str(exc)
        return render_template("home.html", settings=settings, identity=identity, repos=repos, problem=problem)

    @app.get("/browse/")
    def browse_root():
        return redirect(url_for("home"))

    @app.get("/browse/<path:repo_path>")
    def browse(repo_path: str):
        path = paths.clean(repo_path)
        item = jfrog.item(path)
        return render_template("browse.html", settings=settings, item=item, crumbs=paths.crumbs(path),
                               too_big=not item["folder"] and item["size"] > settings.max_download_bytes)

    @app.get("/download/<path:repo_path>")
    def download(repo_path: str):
        path = paths.clean(repo_path)
        item = jfrog.item(path)
        if item["folder"]:
            abort(400, "that path is a folder")
        if item["size"] > settings.max_download_bytes:
            abort(413, f"the file is larger than {_human(settings.max_download_bytes)}")
        upstream = jfrog.open(path)
        limit = settings.max_download_bytes

        def chunks():
            sent = 0
            with upstream:
                while chunk := upstream.read(256 * 1024):
                    sent += len(chunk)
                    if sent > limit:   # grew since it was checked
                        log.warning("download of %s stopped at the size limit", path)
                        return
                    yield chunk
        headers = {"Content-Disposition": f'attachment; filename="{paths.filename(path)}"',
                   "Content-Length": str(item["size"])}
        if item.get("sha256"):
            headers["X-Checksum-Sha256"] = item["sha256"]
        return Response(stream_with_context(chunks()), mimetype=item["mime_type"], headers=headers)

    # -- JSON API --------------------------------------------------------------------------------

    @app.get("/api/v1/identity")
    def api_identity():
        return jsonify(jfrog.identity())

    @app.get("/api/v1/repositories")
    def api_repositories():
        return jsonify(repositories=jfrog.repositories())

    @app.get("/api/v1/items/<path:repo_path>")
    def api_item(repo_path: str):
        return jsonify(jfrog.item(paths.clean(repo_path)))

    return app
