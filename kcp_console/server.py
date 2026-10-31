"""The KCP console's local HTTP server (spec §7): static UI, JSON API and SSE.

Same security model as console/server.py:
- binds 127.0.0.1 unless __main__ is told otherwise;
- every /api call needs X-Console-Token (32 random bytes, handed to the browser
  in the URL fragment), compared with hmac.compare_digest;
- Host must be 127.0.0.1:<port> or localhost:<port>; a cross-origin Origin or
  Sec-Fetch-Site is refused;
- POST/PUT bodies are application/json, at most 1 MiB;
- strict CSP (no inline script or style), nosniff, no-referrer, no framing, no-store;
- static files come from an allow-list, never from a request path;
- every response, SSE frame and log line goes through redact.
"""

from __future__ import annotations

import collections
import os
import hmac
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import diagnose as diag, direct, fleet, knowledge, logic, model, platforms, redact, report
from .checks import artifactory as art_checks, hygiene, preflight as pf, verify as vchecks
from .jfrog import Jfrog, OperatorToken, StaticToken
from .runner import RunError, RunManager, bundle as run_bundle, pod_from_log
from .shell import Shell, load_fixtures
from .sim import engine
from .parse import summary
from .store import RUN_ID_RE, RunStore, StatusStore, Store, StoreError

STATIC_DIR = Path(__file__).parent / "static"
# Page URLs: each section has its own path (old /operate/<page> links still work); all serve index.html.
PAGE_ROUTES = {"/", "/clusters", "/configure"}
# /clusters/<cloud> lists one platform's clusters, /clusters/<cloud>/<name>[/edit] is one cluster;
# /clusters/<name> is the old link to a cluster (the page moves it under its cloud).
CLUSTER_PAGE_RE = re.compile(r"^/clusters/(?:(?:aks|gke|eks|openshift)(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,62}(?:/edit)?)?"
                             r"|[A-Za-z0-9][A-Za-z0-9._-]{0,62})$")
MAX_BODY = 1024 * 1024
MAX_DRAIN = 8 * 1024 * 1024
MAX_SIMS_KEPT = 20
SIM_ID = re.compile(r"^[0-9a-f]{12}$")
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


def sse_frame(kind: str, data) -> str:
    return f"event: {kind}\ndata: {json.dumps(redact.obj(data))}\n\n"


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class KcpServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8766, token: str | None = None, log=sys.stderr,
                 store: Store | None = None, fixtures=None, env=None, poll_seconds: float = 10.0, sleep=time.sleep,
                 base: str = ""):
        self.host, self.requested_port = host, port
        # The path the page is served under when mounted in the unified console (e.g. "/k8s"). The handler
        # still sees unprefixed paths: the portal strips the base before delegating.
        self.base = base.rstrip("/")
        self.token = token or secrets.token_urlsafe(32)
        self.log = log
        self.store = store or Store()
        self.fixtures_path = str(fixtures) if fixtures else None
        self.fixtures = load_fixtures(fixtures) if fixtures else None
        self.mode = "replay" if fixtures else "live"
        self.env = {} if self.mode == "replay" else (env if env is not None else os.environ)
        # No stored JFrog token: live mode mints short-lived ones by OIDC exchange (jfrog.OperatorToken).
        self.tokens = StaticToken("replay-mode-placeholder") if self.mode == "replay" else OperatorToken(self.env)
        self.status = StatusStore(self.store.root)
        self.run_store = RunStore(self.store.root)
        self.runs = RunManager(self.run_store, self.shell, env=self.env, sleep=sleep, poll_seconds=poll_seconds,
                               root=self.store.root, tokens=self.tokens)
        self.read_lock = threading.Lock()
        self._reads = {}  # (name, with_preflight) -> the in-flight read other callers join
        self._reads_lock = threading.Lock()
        self.sims = collections.OrderedDict()
        self.sims_lock = threading.Lock()
        self._httpd = None
        self.port = None

    def shell(self, extra_log=None) -> Shell:
        def log(line):
            self.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {line}") + "\n")
            if extra_log:
                extra_log(line)
        if self.mode == "replay":
            return Shell(mode="replay", fixtures=[dict(f) for f in self.fixtures], log=log)
        return Shell(mode="live", log=log)

    def config(self, name: str) -> dict:
        return model.merged(self.store.get(name))

    def fleet(self) -> dict:
        """Every saved cluster from its last read and its runs: no live calls (fleet.py)."""
        configs = self.store.list()
        statuses = {}
        for c in configs:
            try:
                statuses[c["name"]] = self.status.get(c["name"])
            except Exception:  # noqa: BLE001 - one unreadable status file must not hide the fleet
                statuses[c["name"]] = {}
        rows = fleet.rows(configs, statuses, self.runs.list())
        return {"clusters": rows, "counts": fleet.counts(rows), "platforms": fleet.platforms()}

    def gke_clusters(self) -> dict:
        """The GKE clusters in every project the operator's gcloud login can see (read-only)."""
        out = platforms.gke_list(self.shell(), {(c.get("cluster") or "").lower() for c in self.store.list()})
        if out.get("error"):
            out["error"] = redact.text(out["error"])
        return out

    def aks_clusters(self) -> dict:
        """The AKS clusters the operator's az login can see (read-only), marked when a config already uses one."""
        r = self.shell().run(["az", "aks", "list", "-o", "json"], timeout=90)
        if not r.ok:
            lines = (r.stderr or r.stdout or "").strip().splitlines()
            return {"clusters": [], "error": redact.text(lines[-1] if lines else f"az aks list: exit {r.rc}")}
        used = {(c.get("cluster") or "").lower() for c in self.store.list()}
        acct = self.shell().run(["az", "account", "list", "-o", "json"], timeout=60)
        subs = {a.get("id"): a.get("name", "") for a in (acct.json([]) or [])} if acct.ok else {}
        out = []
        for c in r.json([]) or []:
            parts = (c.get("id") or "").split("/")
            out.append({"name": c.get("name", ""), "resource_group": c.get("resourceGroup", ""), "location": c.get("location", ""),
                        "subscription_id": parts[2] if len(parts) > 2 else "",
                        "subscription_name": subs.get(parts[2], "") if len(parts) > 2 else "", "version": c.get("kubernetesVersion", ""),
                        "power": (c.get("powerState") or {}).get("code", ""),
                        "oidc_issuer": (c.get("oidcIssuerProfile") or {}).get("issuerUrl") or "",
                        "workload_identity": bool(((c.get("securityProfile") or {}).get("workloadIdentity") or {}).get("enabled")),
                        "configured": (c.get("name") or "").lower() in used})
        return {"clusters": sorted(out, key=lambda x: (x["configured"], x["name"]))}

    def discover(self, name: str, with_preflight: bool) -> dict:
        """One read per cluster at a time: a second request for the same cluster joins the read in
        flight and gets its result; reads of other clusters wait their turn (one shell at a time)."""
        key = (name, with_preflight)
        with self._reads_lock:
            job = self._reads.get(key)
            owner = job is None
            if owner:
                job = self._reads[key] = {"done": threading.Event(), "out": None, "err": None}
        if not owner:
            job["done"].wait(timeout=600)
            if job["err"] is not None:
                raise job["err"]
            if job["out"] is None:
                raise HttpError(504, "the cluster read did not finish")
            return job["out"]
        try:
            job["out"] = self._discover(name, with_preflight)
            return job["out"]
        except Exception as exc:
            job["err"] = exc
            raise
        finally:
            with self._reads_lock:
                self._reads.pop(key, None)
            job["done"].set()

    def _discover(self, name: str, with_preflight: bool) -> dict:
        cfg = self.config(name)
        with self.read_lock:
            sh = self.shell()
            kc = direct.paths(Path(self.store.root), name)["kubeconfig"] if self.mode == "live" else None
            # Rendered manifests and values can hold secret-looking strings; store and show them redacted.
            facts = redact.obj(pf.discover(cfg, sh, env=self.env, kubeconfig=kc, tokens=self.tokens))
            out = {"facts": facts, "mode": self.mode}
            patch = {"discover": facts, "discover_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if with_preflight:
                out["preflight"] = pf.preflight(cfg, facts)
                patch["preflight"] = out["preflight"]
            if sh.misses:
                out["fixture_misses"] = sh.misses
            self.status.update(name, patch)
            return out

    def evidence(self, name: str, extra_text: str = "") -> dict:
        cfg = self.config(name)
        st = self.status.get(name)
        runs = self.run_store.all()
        logs = self.run_store.logs()
        b = run_bundle(cfg, runs, logs, st)
        if extra_text:
            if "Node inspected" in extra_text or "QUICK SUMMARY" in extra_text or "RT oidc token url" in extra_text:
                b["kubelet_checks"].append(extra_text)
            pod = pod_from_log(extra_text, {"id": "pasted", "name": "CREATE_TEST_POD", "request": {"parameters": {}}}, cfg)
            if pod:
                b["pods"].append(pod)
        return b

    def analysis(self, name: str, extra_text: str = "") -> dict:
        b = self.evidence(name, extra_text)
        d = diag.diagnose(b)
        st = self.status.get(name)
        checks = b["kubelet_checks"]
        latest = checks[-1] if checks else ""
        nodes = summary.quick_summary(latest)["nodes"] if latest else []
        seen_before = {(n["node"] or "").lower() for c in checks[:-1] for n in summary.quick_summary(c)["nodes"]}
        cfg = b["config"]
        pods = [{**vchecks.verify(p, latest), "time": p["time"], "kind": p["kind"], "run": p.get("run")} for p in b["pods"]]
        return {"verdict": d["verdict"], "findings": d["findings"], "three_layers": d["three_layers"],
                "kubelet_restart": vchecks.kubelet_restart(latest) if latest else [],
                "fresh_nodes": [n["node"] for n in nodes if (n["node"] or "").lower() not in seen_before and len(checks) > 1],
                "pods": pods, "writers": (st.get("discover") or {}).get("daemonsets") or [],
                "chart_rbac": hygiene.chart_rbac(b["manifest"]),
                "cache": {"duration": cfg["values"]["default_cache_duration"],
                          "per_hour": logic.exchanges_per_hour(max(1, len(cfg["cluster"]["nodes"])), 1, len(cfg["values"]["match_images"]),
                                                               logic.duration_seconds(cfg["values"]["default_cache_duration"]) or 0)},
                "checks_count": len(checks), "runs_used": b["runs"],
                "log_leaks": hygiene.log_leaks(self.run_store.logs())}

    def artifactory(self, name: str, action: str, body: dict) -> dict:
        cfg = self.config(name)
        jf = Jfrog(self.shell(), cfg["jfrog"]["host"], tokens=self.tokens)
        j = cfg["jfrog"]
        if action == "probe":
            res = []
            for host in (j["test_repo_host"], j["host"]):
                p = jf.realm_probe(host)
                p["check"] = art_checks.realm(p, j["test_repo"])
                res.append(p)
            self.status.update(name, {"probes": res})
            return {"probes": res}
        if action == "aql":
            prefix = str(body.get("prefix") or "mcp/sonarqube")
            if not re.match(r"^[\w./-]{1,120}$", prefix):
                raise HttpError(400, "prefix: letters, digits, . / - _ only")
            r = jf.aql(prefix)
            hits = (r["json"] or {}).get("results", []) if isinstance(r["json"], dict) else []
            return {"status": r["status"], "results": hits,
                    "local": sorted({h["repo"] for h in hits if not h["repo"].endswith(("-virtual", "-remote"))})}
        if action == "storage":
            r = jf.storage(j["test_repo"], str(body.get("path") or "mcp/sonarqube/1.26-v1"))
            return {"status": r["status"], "json": r["json"]}
        if action == "binary":
            path = cfg["values"]["download_url"].split("/artifactory/" + j["binary_repo"] + "/", 1)[-1] + "-amd64"
            r = jf.storage(j["binary_repo"], path)
            size = int((r["json"] or {}).get("size", 0)) if r["ok"] and isinstance(r["json"], dict) else None
            sha = ((r["json"] or {}).get("checksums") or {}).get("sha256") if isinstance(r["json"], dict) else None
            return {"status": r["status"], "size": size, "sha256": sha, "check": art_checks.binary_integrity(size, sha)}
        if action == "groups":
            r = jf.groups()
            return {"status": r["status"], "check": art_checks.token_scope(r["status"])}
        if action == "remote":
            r = jf.remote_probe("example-docker-remote")
            res = {"status": r["status"], "location": r["headers"].get("location", "")}
            res["check"] = art_checks.remote(res["status"], res["location"])
            self.status.update(name, {"remote": {"status": res["status"], "location": res["location"]}})
            return res
        if action == "audit":
            st = self.status.get(name)
            text = body.get("text")
            if not text:
                r = jf.request_log()
                if not r["ok"]:
                    return {"status": "NOT RUN", "http": r["status"],
                            "detail": f"The request log API answered HTTP {r['status']} (unverified API). Use the manual steps, "
                                      f"then paste the request-log lines here.", "steps": knowledge.AUDIT["manual_steps"]}
                text = r["body"]
            recs = vchecks.parse_request_log(str(text))
            a = self.analysis(name)
            proof = a["verdict"].get("proof") or {}
            pod = next((p for p in self.evidence(name)["pods"] if p.get("node") == proof.get("node") and p["kind"] == "test"
                        and "Successfully pulled" in p["events"]), None)
            window = vchecks.pull_window(pod) if pod else None
            res = vchecks.audit_attribution(recs, j["mapped_user"], window)
            res["window"] = window
            self.status.update(name, {"audit": recs[-200:]})
            return res
        if action == "mappings":
            st = self.status.get(name)
            facts = st.get("discover") or {}
            maps = (facts.get("mappings") or {}).get(model.provider_name(cfg)) or []
            sas = {cfg["k8s"]["namespace"]: [cfg["k8s"]["service_account"]] if (facts.get("service_account") or {}).get("exists") else []}
            return {"provider": model.provider_name(cfg), "mappings": maps, "providers": facts.get("providers") or [],
                    "orphaned": art_checks.orphaned_mappings(maps, sas), "preview": model.mapping_request(cfg),
                    "provider_preview": model.provider_request(cfg), "protected": cfg["jfrog"]["protected_providers"]}
        raise HttpError(404, "no such Artifactory action")

    def session_info(self) -> dict:
        return {"mode": self.mode, "fixtures": self.fixtures_path, "store": str(self.store.root),
                "runbook": knowledge.RUNBOOK, "platforms": model.PLATFORMS,
                "jfrog_token_present": self.tokens.configured}

    # -- simulations: deterministic and fast, so they run inline and are kept for replay over SSE

    def run_sim(self, name: str) -> dict:
        r = engine.run(engine.load(name))
        sim_id = secrets.token_hex(6)
        out = {"sim_id": sim_id, "name": r["name"], "title": r["title"], "story": r["story"], "runbook": r["runbook"],
               "verdict": r["verdict"], "root_cause": r["root_cause"], "chain": r["chain"], "events": r["events"],
               "diagnosis": r["diagnosis"], "expect": engine.load(name).get("expect", {}),
               "mismatches": engine.check(engine.load(name), r),
               "evidence": {k: r["bundle"][k] for k in ("values_file", "helm_values", "kubelet_checks", "pods",
                                                        "install_logs", "reset_logs", "audit", "daemonsets")}}
        with self.sims_lock:
            self.sims[sim_id] = out
            while len(self.sims) > MAX_SIMS_KEPT:
                self.sims.popitem(last=False)
        return out

    def scenarios(self) -> list:
        out = []
        for n in engine.names():
            s = engine.load(n)
            out.append({"name": n, "title": s.get("title"), "story": s.get("story"), "runbook": s.get("runbook"),
                        "expect": s.get("expect", {})})
        return out

    def allowed_hosts(self) -> set:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.host not in ("127.0.0.1", "localhost"):
            hosts.add(f"{self.host}:{self.port}")
        return hosts

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.requested_port), _make_handler(self))
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.static_files = {p.name for p in STATIC_DIR.iterdir() if p.is_file() and p.suffix in CONTENT_TYPES}
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1},
                         name="kcp-console-http", daemon=True).start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _make_handler(console: KcpServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "kcp-console"
        sys_version = ""

        def log_message(self, fmt, *args):
            console.log.write(redact.text(f"{time.strftime('%H:%M:%S')} {fmt % args}") + "\n")

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
                    self.rfile.read(length)
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
                self._json(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _dispatch

        def _static(self, path: str):
            if path == "/favicon.ico":
                self._headers(204, "text/plain", 0)
                return
            name = "index.html" if path.rstrip("/") in PAGE_ROUTES or path == "/" or CLUSTER_PAGE_RE.match(path) else path[len("/static/"):] if path.startswith("/static/") else None
            if name not in console.static_files:
                raise HttpError(404, "not found")
            data = (STATIC_DIR / name).read_bytes()
            # The page carries the token only when the operator opened it directly (typed, bookmarked,
            # reloaded): another site can link here but cannot read the response, and gets no token.
            if name == "index.html" and self.headers.get("Sec-Fetch-Site") in ("none", "same-origin"):
                data = data.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n  <meta name="console-token" content="'
                                    + console.token.encode() + b'">', 1)
            if name == "index.html" and console.base:
                b = console.base.encode()
                data = data.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n  <meta name="console-base" content="' + b + b'">', 1)
                data = data.replace(b'"/static/', b'"' + b + b'/static/')
            self._headers(200, CONTENT_TYPES[Path(name).suffix], len(data))
            self.wfile.write(data)

        def _api(self, method: str, path: str):
            parts = path.strip("/").split("/")[1:]
            if method == "GET" and parts == ["health"]:
                return self._json(200, {"ok": True})
            if method == "GET" and parts == ["session"]:
                return self._json(200, console.session_info())
            if method == "GET" and parts == ["knowledge"]:
                return self._json(200, knowledge.as_dict())
            if method == "GET" and parts == ["defaults"]:
                return self._json(200, model.blank_config())
            if method == "POST" and parts == ["validate"]:
                cfg = self._body().get("config")
                if not isinstance(cfg, dict):
                    raise HttpError(400, "config must be an object")
                return self._json(200, {"issues": model.validate(model.merged(cfg))})
            if parts and parts[0] == "configs":
                return self._configs(method, parts[1:])
            if method == "POST" and parts == ["values"]:
                cfg = self._body().get("config")
                if not isinstance(cfg, dict):
                    raise HttpError(400, "config must be an object")
                cfg = model.merged(cfg)
                live = ""
                if cfg.get("name") and model.NAME_RE.match(cfg["name"]):
                    live = (console.status.get(cfg["name"]).get("discover") or {}).get("helm_values") or ""
                return self._json(200, {"text": model.render_values(cfg), "sha256": model.values_sha256(cfg),
                                        "path": cfg["ado"]["values_path"], "live": live,
                                        "issues": model.validate(cfg), "template_issues": model.validate_template(cfg),
                                        "provider_request": model.provider_request(cfg), "mapping_request": model.mapping_request(cfg),
                                        "test_pod": model.test_pod_yaml(cfg), "provider_name": model.provider_name(cfg)})
            if method == "GET" and parts == ["fleet"]:
                return self._json(200, console.fleet())
            if method == "GET" and parts == ["aks"]:
                return self._json(200, console.aks_clusters())
            if method == "GET" and parts == ["gke"]:
                return self._json(200, console.gke_clusters())
            if method == "POST" and parts in (["discover"], ["preflight"]):
                name = self._name(self._body().get("config"))
                return self._json(200, console.discover(name, parts[0] == "preflight"))
            if parts and parts[0] == "runs":
                return self._runs(method, parts[1:])
            if method == "POST" and parts == ["diagnose"]:
                body = self._body()
                text = body.get("text") or ""
                if not isinstance(text, str) or len(text) > 512 * 1024:
                    raise HttpError(400, "text must be a string under 512 KiB")
                return self._json(200, console.analysis(self._name(body.get("config")), text))
            if method == "GET" and parts == ["nodes"]:
                return self._json(200, console.analysis(self._name(self._query("config"))))
            if method == "POST" and len(parts) == 2 and parts[0] == "artifactory":
                body = self._body()
                return self._json(200, console.artifactory(self._name(body.get("config")), parts[1], body))
            if method == "POST" and parts == ["verify", "audit"]:
                body = self._body()
                return self._json(200, console.artifactory(self._name(body.get("config")), "audit", body))
            if len(parts) == 2 and parts[0] == "status":
                name = self._name(parts[1])
                if method == "GET":
                    return self._json(200, {"status": console.status.get(name)})
                if method == "PUT":
                    patch = self._body().get("status")
                    if not isinstance(patch, dict) or set(patch) - {"open_items", "security", "demo", "notes"}:
                        raise HttpError(400, "status may set open_items, security, demo and notes")
                    try:
                        return self._json(200, {"status": console.status.update(name, patch)})
                    except StoreError as exc:
                        raise HttpError(400, str(exc)) from None
            if method == "GET" and len(parts) == 2 and parts[0] == "report":
                name = self._name(parts[1])
                cfg = console.config(name)
                st = console.status.get(name)
                d = diag.diagnose(console.evidence(name))
                rep = report.build(cfg, st, d, st.get("preflight"), [r for r in console.runs.list() if r.get("config") == name])
                if self._query("format") == "json":
                    return self._json(200, rep)
                data = report.markdown(rep).encode()
                self._headers(200, "text/markdown; charset=utf-8", len(data),
                              {"Content-Disposition": f'attachment; filename="kcp-report-{name}.md"'})
                self.wfile.write(data)
                return None
            if method == "GET" and parts == ["sim", "scenarios"]:
                return self._json(200, {"scenarios": console.scenarios()})
            if method == "POST" and parts == ["sim"]:
                name = str(self._body().get("scenario") or "")
                if not SCENARIO_NAME.match(name) or name not in engine.names():
                    raise HttpError(400, f"no built-in scenario named {name!r}")
                return self._json(200, console.run_sim(name))
            if method == "GET" and len(parts) in (2, 3) and parts[0] == "sim":
                if not SIM_ID.match(parts[1]):
                    raise HttpError(404, "no such simulation")
                with console.sims_lock:
                    sim = console.sims.get(parts[1])
                if sim is None:
                    raise HttpError(404, "no such simulation")
                if len(parts) == 2:
                    return self._json(200, sim)
                if parts[2] == "events":
                    return self._stream(sim)
            raise HttpError(404, "no such endpoint")

        def _query(self, key):
            return (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get(key) or [""])[0]

        def _name(self, name):
            if not isinstance(name, str) or not model.NAME_RE.match(name):
                raise HttpError(400, "config must be a saved config name")
            try:
                console.store.get(name)
            except FileNotFoundError:
                raise HttpError(404, "no such config") from None
            return name

        def _runs(self, method: str, rest: list):
            try:
                if not rest:
                    if method == "GET":
                        return self._json(200, {"runs": [console.runs.public(r) for r in console.runs.list()]})
                    if method == "POST":
                        body = self._body()
                        name = self._name(body.get("config"))
                        params = body.get("params") or {}
                        if not isinstance(params, dict):
                            raise HttpError(400, "params must be an object")
                        run = console.runs.prepare(console.config(name), str(body.get("kind") or ""), str(body.get("name") or ""),
                                                   params, console.status.get(name))
                        return self._json(200, run)
                    raise HttpError(405, "method not allowed")
                if not RUN_ID_RE.match(rest[0]):
                    raise HttpError(404, "no such run")
                run = console.runs.get(rest[0])
                if method == "GET" and len(rest) == 1:
                    return self._json(200, console.runs.public(run))
                if method == "GET" and rest[1:] == ["log"]:
                    return self._json(200, {"log": console.run_store.log(run["id"])})
                if method == "GET" and rest[1:] == ["events"]:
                    return self._stream_run(run["id"])
                if method == "POST" and rest[1:] == ["approve"]:
                    body = self._body()
                    return self._json(200, console.runs.approve(run["id"], str(body.get("request_sha256") or ""),
                                                                str(body.get("typed_cluster") or ""), body.get("approved") is True))
                if method == "POST" and rest[1:] == ["cancel"]:
                    return self._json(200, console.runs.cancel(run["id"]))
                raise HttpError(404, "no such endpoint")
            except RunError as exc:
                raise HttpError(exc.status, str(exc)) from None

        def _stream_run(self, run_id):
            self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no"})
            self.close_connection = True
            sent = 0
            while True:
                with console.runs.cond:
                    run = console.runs.runs[run_id]
                    if sent >= len(run.get("events", [])) and run["status"] in ("queued", "running"):
                        console.runs.cond.wait(15)
                    batch = run.get("events", [])[sent:]
                    status = run["status"]
                sent += len(batch)
                out = "".join(sse_frame("event", ev) for ev in batch) or ": keep-alive\n\n"
                done = status not in ("queued", "running")
                if done:
                    out += sse_frame("done", console.runs.public(console.runs.get(run_id)))
                self.wfile.write(out.encode())
                self.wfile.flush()
                if done:
                    return

        def _configs(self, method: str, rest: list):
            try:
                if method == "GET" and not rest:
                    return self._json(200, {"configs": console.store.list()})
                if len(rest) != 1:
                    raise HttpError(404, "no such endpoint")
                if method == "GET":
                    return self._json(200, {"config": console.store.get(rest[0])})
                if method == "PUT":
                    cfg = self._body().get("config")
                    if not isinstance(cfg, dict):
                        raise HttpError(400, "config must be an object")
                    cfg = console.store.put(rest[0], cfg)
                    return self._json(200, {"config": cfg, "issues": model.validate(model.merged(cfg))})
                if method == "DELETE":
                    if not console.store.delete(rest[0]):
                        raise HttpError(404, "no such config")
                    return self._json(200, {"deleted": rest[0]})
            except FileNotFoundError:
                raise HttpError(404, "no such config") from None
            except StoreError as exc:
                raise HttpError(400, str(exc)) from None
            raise HttpError(405, "method not allowed")

        def _stream(self, sim: dict):
            """Replays a finished simulation's events in order, then its verdict."""
            self._headers(200, "text/event-stream; charset=utf-8", extra={"X-Accel-Buffering": "no"})
            self.close_connection = True
            out = "".join(sse_frame("event", ev) for ev in sim["events"])
            out += sse_frame("done", {"verdict": sim["verdict"], "root_cause": sim["root_cause"], "chain": sim["chain"]})
            self.wfile.write(out.encode())
            self.wfile.flush()

    return Handler


# The unified console (portal/) mounts this console's handler under its base path.
make_handler = _make_handler
