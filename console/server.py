"""The console's local HTTP server (spec §6): static UI, JSON API and SSE.

Security model (the console runs with the operator's cloud logins):
- Binds to 127.0.0.1 unless explicitly overridden (see __main__.check_bind).
- Every /api call needs X-Console-Token, a 32-byte random value generated at
  start and handed to the browser in the URL fragment (never sent to a server,
  never logged). Compared with hmac.compare_digest.
- Host must be 127.0.0.1:<port> or localhost:<port> (DNS-rebinding defence);
  a cross-origin Origin header is refused.
- POST bodies must be application/json and at most 1 MiB.
- Strict CSP (no inline script or style), nosniff, no-referrer, no framing.
- Everything sent over SSE or logged goes through redact.
"""

from __future__ import annotations

import collections
import hmac
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import inventory, model, redact
from .runner import STEPS, RunError, RunManager
from .shell import Shell, load_fixtures
from .sim import scenario as sc
from .sim.engine import Simulation
from .steps import discover, preflight
from .store import Store, StoreError

STATIC_DIR = Path(__file__).parent / "static"
# Page URLs: /gcp and /azure, and each tab under them; all serve index.html.
PAGE_ROUTES = {"/gcp", "/azure"} | {f"/{c}/{t}" for c in ("gcp", "azure") for t in ("configs", "configure", "runs", "simulate")}
# Replay mode never runs the real terraform: plans and applies go to this fake,
# which writes nothing outside the workspace and touches no provider.
FAKE_TERRAFORM = Path(__file__).parent / "tests" / "fixtures" / "fake_terraform"
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
MAX_BODY = 1024 * 1024
MAX_DRAIN = 8 * 1024 * 1024
MAX_SIMS_KEPT = 20
MAX_SIMS_QUEUED = 8
SSE_HEARTBEAT = 15.0
SCENARIO_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

# Only these config paths can be changed from the Simulate tab: they are the
# knobs that decide outcomes, and nothing else reaches the emulated world.
SIM_OVERRIDES = {
    "pull_path.enabled", "pull_path.ttl_seconds", "pull_path.rotation_cron", "pull_path.alert_after_seconds",
    "pull_path.min_ttl_seconds", "pull_path.function_app_refresh", "runtime.ttl_seconds",
    "sim.runtime_audience", "sim.access_token_version", "sim.app_role_gate",
}


def sse_frame(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(redact.obj(data), separators=(',', ':'))}\n\n"


class HttpError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------- simulations


class SimJob:
    def __init__(self, scenario: dict):
        self.id = uuid.uuid4().hex[:16]
        self.scenario = scenario
        self.status = "queued"
        self.events: list = []
        self.result = None
        self.error = None
        self.created = time.time()
        self.cond = threading.Condition()

    def add_event(self, ev: dict) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self, status: str, result=None, error=None) -> None:
        with self.cond:
            self.status, self.result, self.error = status, result, error
            self.cond.notify_all()


class SimManager:
    """One worker thread: simulations share process state, so they run one at a time."""

    def __init__(self):
        self.jobs: "collections.OrderedDict[str, SimJob]" = collections.OrderedDict()
        self.lock = threading.Lock()
        self.queue: "queue.Queue[SimJob | None]" = queue.Queue()
        self.worker = threading.Thread(target=self._work, name="sim-worker", daemon=True)
        self.worker.start()

    def submit(self, scenario: dict) -> SimJob:
        with self.lock:
            pending = sum(1 for j in self.jobs.values() if j.status in ("queued", "running"))
            if pending >= MAX_SIMS_QUEUED:
                raise HttpError(429, "too many simulations queued; wait for one to finish")
            job = SimJob(scenario)
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_SIMS_KEPT:
                oldest = next(iter(self.jobs.values()))
                if oldest.status in ("queued", "running"):
                    break
                self.jobs.popitem(last=False)
        self.queue.put(job)
        return job

    def get(self, sim_id: str):
        with self.lock:
            return self.jobs.get(sim_id)

    def _work(self):
        while True:
            job = self.queue.get()
            if job is None:
                return
            with job.cond:
                job.status = "running"
                job.cond.notify_all()
            try:
                result = Simulation(job.scenario, on_event=job.add_event).run()
                job.finish("done", result=redact.obj(result))
            except Exception as exc:  # noqa: BLE001 - report to the UI, keep the worker alive
                job.finish("error", error=redact.text(f"{type(exc).__name__}: {exc}"))

    def stop(self):
        self.queue.put(None)


def _scenario_catalogue() -> dict:
    out = []
    for s in sc.builtin_summaries():
        cfg = sc.load(s["name"])["config"]
        s["settings"] = {
            "pull_path.ttl_seconds": cfg["pull_path"]["ttl_seconds"],
            "pull_path.rotation_cron": cfg["pull_path"]["rotation_cron"],
            "pull_path.alert_after_seconds": cfg["pull_path"]["alert_after_seconds"],
            "pull_path.min_ttl_seconds": cfg["pull_path"]["min_ttl_seconds"],
            "runtime.ttl_seconds": cfg["runtime"]["ttl_seconds"],
        }
        out.append(s)
    return {"scenarios": out, "fault_types": sc.FAULT_TYPES, "default_load": sc.DEFAULT_LOAD,
            "allowed_overrides": sorted(SIM_OVERRIDES)}


# --------------------------------------------------------------------- server


class ConsoleServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, token: str | None = None, log=sys.stderr,
                 store: Store | None = None, fixtures=None, record_dir=None, delivery=None, repo_root=None):
        self.host = host
        self.requested_port = port
        self.token = token or secrets.token_urlsafe(32)
        self.log = log
        self.sims = SimManager()
        self.store = store or Store()
        # Cloud/JFrog calls: live by default; replay answers from recorded
        # fixtures (demos, tests); record runs live and saves redacted copies.
        self.fixtures = load_fixtures(fixtures) if fixtures else None
        self.record_dir = Path(record_dir) if record_dir else None
        self.mode = "replay" if fixtures else "record" if record_dir else "live"
        self.cloud_lock = threading.Lock()
        self.delivery = delivery
        self.runs = RunManager(self.store.root, cloud_shell=self._run_shell, tf_shell=self._tf_shell,
                               env=self.step_env(), simulate=self.simulate_config, store=self.store,
                               delivery=delivery, repo_root=repo_root)
        self.repo_root = repo_root
        self._catalogue = None
        self._httpd = None
        self.port = None

    def shell(self) -> Shell:
        def log(line):
            self.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {line}") + "\n")
        if self.mode == "replay":
            return Shell(mode="replay", fixtures=list(self.fixtures), log=log)
        return Shell(mode=self.mode, log=log)

    def step_env(self):
        """Environment the steps read JFROG_ACCESS_TOKEN from. Replay mode answers every
        JFrog call from fixtures and never runs real terraform, so it uses a placeholder."""
        if self.mode == "replay":
            return {"JFROG_ACCESS_TOKEN": "replay-mode-placeholder"}
        return os.environ

    def _run_shell(self, run_log) -> Shell:
        def log(line):
            run_log(line)
            self.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {line}") + "\n")
        if self.mode == "replay":
            return Shell(mode="replay", fixtures=[dict(f) for f in self.fixtures], log=log)
        return Shell(mode=self.mode, log=log)

    def _tf_shell(self, run_log) -> Shell:
        sh = self._run_shell(run_log)
        if self.mode == "replay":
            sh.mode = "live"
            sh.which = lambda n: str(FAKE_TERRAFORM) if n == "terraform" else None
        return sh

    def simulate_config(self, cfg: dict) -> dict:
        """The pipeline's Simulate step: the config for 24 h, on the shared simulation worker."""
        job = self.sims.submit(sc.from_config(cfg, duration="24h", seed=1))
        deadline = time.monotonic() + 300
        with job.cond:
            while job.status in ("queued", "running"):
                if not job.cond.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("the simulation did not finish within 5 minutes")
        if job.status != "done":
            raise RuntimeError(f"simulation failed: {job.error}")
        r = job.result
        return {"verdict": {"status": r.get("verdict"), "root_cause": r.get("root_cause"), "chain": r.get("chain")},
                "summary": r.get("summary")}

    def save_recording(self, sh: Shell, op: str) -> None:
        if self.mode == "record" and sh.recorded:
            sh.save_fixtures(self.record_dir / f"{time.strftime('%Y%m%dT%H%M%S')}-{op}.json")

    def session_info(self) -> dict:
        return {"mode": self.mode, "jfrog_token_present": bool(os.environ.get("JFROG_ACCESS_TOKEN")),
                "azure_config_dir": os.environ.get("AZURE_CONFIG_DIR") or "~/.azure (default)",
                "gcloud_config": os.environ.get("CLOUDSDK_CONFIG") or "~/.config/gcloud (default)",
                "store": str(self.store.root),
                "terraform": "fake (replay mode: nothing is planned or applied for real)" if self.mode == "replay" else "terraform on PATH",
                "steps": STEPS}

    @property
    def catalogue(self) -> dict:
        if self._catalogue is None:
            self._catalogue = _scenario_catalogue()
        return self._catalogue

    def allowed_hosts(self) -> set:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.host not in ("127.0.0.1", "localhost"):
            hosts.add(f"{self.host}:{self.port}")
        return hosts

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    def start(self) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.requested_port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.static_files = {p.name for p in STATIC_DIR.iterdir() if p.is_file() and p.suffix in CONTENT_TYPES}
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                         name="console-http", daemon=True).start()

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.runs.shutdown()
        self.sims.stop()


def _make_handler(console: ConsoleServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "jfrog-oidc-console"
        sys_version = ""

        def log_message(self, fmt, *args):
            console.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {fmt % args}") + "\n")

        # -- plumbing --------------------------------------------------------

        def _headers(self, status: int, ctype: str, length=None, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            if length is not None:
                self.send_header("Content-Length", str(length))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, status: int, body) -> None:
            data = json.dumps(redact.obj(body)).encode()
            self._headers(status, "application/json", len(data))
            self.wfile.write(data)

        def _body(self) -> dict:
            ctype = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                if length <= MAX_DRAIN:
                    self.rfile.read(length)  # drain so the client sees the status
                else:
                    self.close_connection = True
                raise HttpError(413, "request body too large")
            if ctype.split(";")[0].strip().lower() != "application/json":
                if length:
                    self.rfile.read(length)
                raise HttpError(415, "Content-Type must be application/json")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise HttpError(400, "invalid JSON") from None
            if not isinstance(body, dict):
                raise HttpError(400, "JSON body must be an object")
            return body

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in console.allowed_hosts()

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                site = self.headers.get("Sec-Fetch-Site", "same-origin")
                if site in ("same-origin", "none"):
                    return True
                # A top-level page load from another site (e.g. localhost -> 127.0.0.1)
                # gets only the static shell; the API still refuses it.
                return (self.command == "GET" and self.headers.get("Sec-Fetch-Mode") == "navigate"
                        and not self.path.startswith("/api/"))
            return origin in {f"http://{h}" for h in console.allowed_hosts()}

        def _token_ok(self) -> bool:
            given = self.headers.get("X-Console-Token", "")
            return bool(given) and hmac.compare_digest(given.encode(), console.token.encode())

        def _dispatch(self):
            try:
                if not self._host_ok():
                    raise HttpError(403, "bad Host header")
                if not self._origin_ok():
                    raise HttpError(403, "cross-origin request refused")
                path = urllib.parse.urlsplit(self.path).path
                if path.startswith("/api/"):
                    if not self._token_ok():
                        raise HttpError(401, "missing or invalid X-Console-Token")
                    return self._api(self.command, path)
                if self.command != "GET":
                    raise HttpError(405, "method not allowed")
                return self._static(path)
            except HttpError as exc:
                self._json(exc.status, {"error": str(exc), **({"code": exc.code} if exc.code else {})})
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _dispatch

        def _static(self, path: str):
            if path == "/favicon.ico":
                self._headers(204, "text/plain", 0)
                return
            name = "index.html" if path.rstrip("/") in PAGE_ROUTES or path == "/" else path[len("/static/"):] if path.startswith("/static/") else None
            if name not in console.static_files:  # allow-list: no request path ever reaches the filesystem
                raise HttpError(404, "not found")
            data = (STATIC_DIR / name).read_bytes()
            # The page carries the token only when the operator opened it directly (typed, bookmarked,
            # reloaded): another site can link here but cannot read the response, and gets no token.
            if name == "index.html" and self.headers.get("Sec-Fetch-Site") in ("none", "same-origin"):
                data = data.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n  <meta name="console-token" content="'
                                    + console.token.encode() + b'">', 1)
            self._headers(200, CONTENT_TYPES[Path(name).suffix], len(data))
            self.wfile.write(data)

        # -- API -------------------------------------------------------------

        def _api(self, method: str, path: str):
            parts = path.strip("/").split("/")[1:]
            if method == "GET" and parts == ["health"]:
                return self._json(200, {"ok": True})
            if method == "GET" and parts == ["schema"]:
                return self._json(200, model.schema())
            if method == "POST" and parts == ["validate"]:
                cfg = self._body().get("config")
                if not isinstance(cfg, dict):
                    raise HttpError(400, "config must be an object")
                return self._json(200, model.validate(cfg))
            if method == "GET" and parts == ["session"]:
                return self._json(200, console.session_info())
            if parts and parts[0] == "configs":
                return self._configs(method, parts[1:])
            if method == "GET" and len(parts) == 2 and parts[0] == "inventory" and parts[1] in ("gcp", "azure"):
                return self._inventory(parts[1], (self._query().get("scope") or [None])[0])
            if method == "POST" and parts in (["preflight"], ["discover"]):
                return self._cloud_step(parts[0], self._config_from_body())
            if parts and parts[0] == "runs":
                return self._runs(method, parts[1:])
            if method == "GET" and parts == ["sim", "scenarios"]:
                return self._json(200, console.catalogue)
            if method == "POST" and parts == ["sim"]:
                return self._start_sim(self._body())
            if method == "GET" and len(parts) == 2 and parts[0] == "sim":
                job = self._job(parts[1])
                with job.cond:
                    return self._json(200, {"sim_id": job.id, "status": job.status, "error": job.error,
                                            "result": job.result, "events": len(job.events)})
            if method == "GET" and len(parts) == 3 and parts[0] == "sim" and parts[2] == "events":
                return self._stream(self._job(parts[1]))
            raise HttpError(404, "no such endpoint")

        def _query(self) -> dict:
            return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def _config_from_body(self) -> dict:
            cfg = self._body().get("config")
            if not isinstance(cfg, dict):
                raise HttpError(400, "config must be an object")
            kind = model.get_path(cfg, "target.kind")
            if kind not in model.KINDS:
                raise HttpError(400, f"unknown target kind {kind!r}")
            return cfg

        def _configs(self, method: str, rest: list):
            try:
                if method == "GET" and not rest:
                    return self._json(200, {"configs": console.store.list()})
                if len(rest) != 1:
                    raise HttpError(404, "no such endpoint")
                name = rest[0]
                if method == "GET":
                    return self._json(200, {"config": console.store.get(name)})
                if method == "PUT":
                    cfg = console.store.put(name, self._config_from_body())
                    return self._json(200, {"config": cfg, "validation": model.validate(cfg)})
                if method == "DELETE":
                    if not console.store.delete(name):
                        raise HttpError(404, "no such config")
                    return self._json(200, {"deleted": name, **console.runs.delete_for(name)})
            except FileNotFoundError:
                raise HttpError(404, "no such config") from None
            except StoreError as exc:
                raise HttpError(400, str(exc)) from None
            raise HttpError(405, "method not allowed")

        def _inventory(self, cloud: str, scope):
            if not console.cloud_lock.acquire(timeout=30):
                raise HttpError(409, "another cloud operation is still running")
            try:
                sh = console.shell()
                saved = []
                for c in console.store.list():
                    try:
                        saved.append(console.store.get(c["name"]))
                    except (FileNotFoundError, StoreError, ValueError):
                        continue
                result = inventory.run(cloud, sh, console.repo_root, scope=scope, configs=saved)
                console.save_recording(sh, "inventory")
                return self._json(200, result)
            finally:
                console.cloud_lock.release()

        def _cloud_step(self, step: str, cfg: dict):
            # One cloud operation at a time: they share CLI logins and caches.
            if not console.cloud_lock.acquire(timeout=1):
                raise HttpError(409, "another Preflight or Discover is still running")
            try:
                sh = console.shell()
                result = (preflight.run(cfg, sh, env=console.step_env(), delivery=console.delivery or False)
                          if step == "preflight" else discover.run(cfg, sh))
                console.save_recording(sh, step)
                result["mode"] = console.mode
                if sh.misses:
                    result["fixture_misses"] = sh.misses
                return self._json(200, result)
            finally:
                console.cloud_lock.release()

        def _runs(self, method: str, rest: list):
            try:
                if not rest:
                    if method == "GET":
                        return self._json(200, {"runs": console.runs.list()})
                    if method == "POST":
                        body = self._body()
                        steps = body.get("steps")
                        if steps is not None and (not isinstance(steps, list) or not all(isinstance(x, str) for x in steps)):
                            raise HttpError(400, "steps must be a list of step names")
                        cfg = body.get("config")
                        if not isinstance(cfg, dict):
                            raise HttpError(400, "config must be an object")
                        if model.get_path(cfg, "target.kind") not in model.KINDS:
                            raise HttpError(400, "unknown target kind")
                        run = console.runs.start(cfg, steps)
                        return self._json(202, {"run_id": run.id})
                    raise HttpError(405, "method not allowed")
                if not RUN_ID.match(rest[0]):
                    raise HttpError(404, "no such run")
                run = console.runs.get(rest[0])
                if method == "GET" and len(rest) == 1:
                    return self._json(200, run.snapshot())
                if method == "GET" and rest[1:] == ["events"]:
                    return self._stream_run(run)
                if method == "POST" and rest[1:] == ["approve"]:
                    body = self._body()
                    payload = {k: body[k] for k in ("plan_sha256", "typed_confirmation", "diff_sha256")
                               if isinstance(body.get(k), str)}
                    return self._json(200, console.runs.approve(run.id, str(body.get("step", "")), payload))
                if method == "POST" and rest[1:] == ["cancel"]:
                    return self._json(200, console.runs.cancel(run.id))
                raise HttpError(404, "no such endpoint")
            except RunError as exc:
                raise HttpError(exc.status, str(exc), exc.code) from None

        def _stream_run(self, run):
            self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no"})
            self.close_connection = True
            sent = 0
            self.wfile.write(sse_frame("snapshot", run.snapshot()).encode())
            self.wfile.flush()
            while True:
                with run.cond:
                    if sent >= len(run.events) and run.status in ("running", "waiting_approval"):
                        run.cond.wait(SSE_HEARTBEAT)
                    batch = run.events[sent:]
                    status = run.status
                sent += len(batch)
                out = "".join(sse_frame("event", ev) for ev in batch)
                if not batch and status in ("running", "waiting_approval"):
                    out = ": keep-alive\n\n"
                finished = status not in ("running", "waiting_approval") and sent >= len(run.events)
                if finished:
                    out += sse_frame("done", run.snapshot())
                if out:
                    self.wfile.write(out.encode())
                    self.wfile.flush()
                if finished:
                    return

        def _job(self, sim_id: str) -> SimJob:
            job = console.sims.get(sim_id)
            if job is None:
                raise HttpError(404, "no such simulation")
            return job

        def _start_sim(self, body: dict):
            overrides = body.get("overrides") or {}
            if not isinstance(overrides, dict):
                raise HttpError(400, "overrides must be an object")
            bad = sorted(set(overrides) - SIM_OVERRIDES)
            if bad:
                raise HttpError(400, f"overrides not allowed: {', '.join(bad)}")
            kw = {"overrides": overrides, "seed": body.get("seed"), "load": body.get("load") or None,
                  "duration": body.get("duration")}
            if body.get("faults") is not None:
                if not isinstance(body["faults"], list):
                    raise HttpError(400, "faults must be a list")
                kw["faults"] = body["faults"]
            try:
                if body.get("scenario"):
                    name = str(body["scenario"])
                    # Built-in names only: never let the API name a file on disk.
                    if not SCENARIO_NAME.match(name) or name not in sc.builtin_names():
                        raise ValueError(f"no built-in scenario named {name!r}")
                    scen = sc.load(name, **kw)
                elif isinstance(body.get("config"), dict):
                    errors = model.validate(body["config"])["errors"]
                    if errors:
                        raise ValueError("; ".join(f"{e['path']}: {e['message']}" for e in errors))
                    scen = sc.from_config(body["config"], **kw)
                else:
                    raise ValueError("give a built-in 'scenario' name or a 'config'")
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise HttpError(400, str(exc)) from None
            job = console.sims.submit(scen)
            return self._json(202, {"sim_id": job.id})

        def _stream(self, job: SimJob):
            self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no"})
            self.close_connection = True
            sent = 0
            self.wfile.write(sse_frame("status", {"status": job.status, "scenario": job.scenario["name"],
                                                  "duration_s": job.scenario["duration_s"]}).encode())
            self.wfile.flush()
            while True:
                with job.cond:
                    if sent >= len(job.events) and job.status in ("queued", "running"):
                        job.cond.wait(SSE_HEARTBEAT)
                    batch = job.events[sent:]
                    status, result, error = job.status, job.result, job.error
                sent += len(batch)
                out = "".join(sse_frame("event", ev) for ev in batch)
                if not batch and status in ("queued", "running"):
                    out = ": keep-alive\n\n"
                if status == "done":
                    out += sse_frame("done", {"verdict": result["verdict"], "summary": result["summary"]})
                elif status == "error":
                    out += sse_frame("failed", {"error": error})
                if out:
                    self.wfile.write(out.encode())
                    self.wfile.flush()
                if status in ("done", "error"):
                    return

    return Handler
