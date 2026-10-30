"""Discrete-event simulation of one target's token lifecycle (spec §7.3).

The engine runs the *real* jfrog_oidc client and rotator/rotate.py, unmodified
apart from clock/sleep injection, against in-process emulators on a virtual
clock. Event sources: rotator cron ticks, platform cold starts (Poisson),
runtime requests, fault windows, and alert evaluation every 15 min.

Modelling choices (read these before trusting a verdict):
- Runtime requests are bucketed per minute: the newest warm instance makes one
  real call (token() + an Artifactory storage read) per minute that has any
  requests, and the outcome is weighted by the number of requests that minute.
- Requests are spread over the warm instances (seeded random choice). Each
  cold start adds an instance that lives load.instance_lifetime_minutes, so
  instances that started before an outage keep serving their cached token
  while new ones cannot get one. A cold start whose pull fails adds no instance.
- AR caching is modelled as a per-pull probability (load.uncached_pull_ratio)
  that the artifact is not yet cached; cached pulls never contact JFrog.
- Container Apps picks up a new Key Vault version 5–30 min after it is
  written (uniform, seeded). Premium Functions resolve the reference at
  rotation time when the rotator's ARM refresh succeeds, otherwise 24 h after
  the last resolution.
- Before clock_start the rotator runs once at the previous cron tick and the
  platforms load whatever that produced, so a day starts in steady state.

The process environment, rotate's endpoint globals and stdout are shared, so
only one simulation runs at a time per process (SIM_LOCK).
"""

from __future__ import annotations

import contextlib
import heapq
import io
import itertools
import json
import logging
import math
import os
import random
import secrets
import sys
import threading
import time
import urllib.error
from pathlib import Path

from .. import model, redact
from .clock import VirtualClock
from .emulators.azure import AzureEmulator
from .emulators.gcp import GOOGLE_ISSUER, GcpEmulator
from .emulators.jfrog import SERVICES, JFrogEmulator
from .emulators.platforms import ArPlatform, FlexPlatform, KvPlatform
from .emulators.server import EmulatorServer

SIM_LOCK = threading.Lock()
ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDER = "bootstrap-placeholder"   # what Terraform writes as version 1
GCP_AUDIENCE = "jfrog-gcp"
TENANT = "72f988bf-0000-4000-8000-000000000001"
APP_ID = "5d1c2b3a-0000-4000-8000-00000000a0d1"
RUNTIME_ID = {"client": "c0ffee00-0000-4000-8000-000000000011", "principal": "0b1d0000-0000-4000-8000-000000000011"}
ROTATOR_ID = {"client": "c0ffee00-0000-4000-8000-000000000022", "principal": "0b1d0000-0000-4000-8000-000000000022"}
ALERT_EVERY = 900
ACA_LAG = (5 * 60, 30 * 60)
PREMIUM_CACHE = 24 * 3600

# Every variable the client or rotator reads. Cleared and restored around each
# call so the operator's own environment never leaks into a simulation.
_ENV_KEYS = (
    "JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SOURCE", "JFROG_OIDC_TOKEN_FILE",
    "JFROG_OIDC_ALLOW_HTTP", "JFROG_PROJECT_KEY", "GCE_METADATA_HOST", "IDENTITY_ENDPOINT", "IDENTITY_HEADER",
    "MSI_ENDPOINT", "AZURE_CLIENT_ID", "AZURE_FEDERATED_TOKEN_FILE", "K_SERVICE", "CLOUD_RUN_JOB", "FUNCTION_TARGET",
    "ROTATOR_TARGET", "GCP_SECRET", "AR_REPOSITORIES", "AR_UPDATE_MASK", "KEEP_VERSIONS", "MIN_TTL_SECONDS",
    "DRY_RUN", "KEY_VAULT_URI", "KEY_VAULT_SECRET", "FUNCTION_APP_IDS", "SECRET_MANAGER_API",
    "ARTIFACT_REGISTRY_API", "ARM_API", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
    "all_proxy", "NO_PROXY", "no_proxy",
)


def _ensure_import_paths():
    for p in (ROOT / "client" / "src", ROOT / "rotator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


_ensure_import_paths()
import rotate  # noqa: E402  (rotator/rotate.py, the real one)
from jfrog_oidc import ExchangeError, IdentityError, JFrogOidcError, JFrogTokenProvider, get_json  # noqa: E402


@contextlib.contextmanager
def _workload_env(env: dict):
    unknown = set(env) - set(_ENV_KEYS)
    assert not unknown, f"add {unknown} to _ENV_KEYS"
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # normal approximation keeps this O(1) for heavy load
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _transient(status) -> bool:
    return status is None or status == 0 or status == 429 or status >= 500


class Simulation:
    def __init__(self, scenario: dict, on_event=None):
        self.sc = scenario
        self.cfg = scenario["config"]
        self.kind = self.cfg["target"]["kind"]
        self.cloud = model.cloud_of(self.kind)
        self.start = scenario["start"]
        self.end = self.start + scenario["duration_s"]
        self.rng = random.Random(scenario["seed"])
        self.on_event = on_event
        self.pull_enabled = bool(self.cfg["pull_path"].get("enabled")) and self.kind != "azure_functions_flex"
        self.cron = model.Cron(self.cfg["pull_path"]["rotation_cron"]) if self.pull_enabled else None

        self.events: list = []
        self.rotations: list = []
        self.tokens: list = []      # pull credential versions: {version, start, end}
        self.holds: list = []       # {t, version}: what the platform pulls with
        self.pulls: list = []
        self.requests: list = []
        self.alerts: list = []      # transitions {t, firing}
        self.fault_log: list = []   # {t, type, open}
        self.cred_info: dict = {}   # version label -> {written, exp}
        self.last_success = None
        self.alert_firing = False
        self.instances: list = []
        self._queue: list = []
        self._seq = itertools.count()
        self._last_hold = None

    # ------------------------------------------------------------- helpers

    def _rel(self, t: float) -> float:
        return round(t - self.start, 3)

    def hhmm(self, t: float) -> str:
        day = math.floor((t - self.start) / 86400)
        stamp = time.strftime("%H:%M", time.gmtime(t))
        return stamp if day == 0 else f"{stamp} (day {day:+d})"

    def _emit(self, t: float, etype: str, message: str, **data) -> None:
        ev = {"t": self._rel(t), "type": etype, "message": redact.text(message)}
        if data:
            ev["data"] = redact.obj(data)
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)

    def _push(self, t: float, kind: str, data=None) -> None:
        heapq.heappush(self._queue, (t, next(self._seq), kind, data))

    def _in_outage(self, t: float) -> bool:
        return any(f["type"] == "jfrog_outage" and f["start"] <= t < f["end"] for f in self._windows)

    def _faults_at(self, t: float) -> list:
        return sorted({f["type"] for f in self._windows if f["start"] <= t < f["end"]})

    # --------------------------------------------------------------- setup

    def _build(self):
        cfg, kind = self.cfg, self.kind
        sim = cfg.get("sim", {})
        g_key, a_key = secrets.token_bytes(16), secrets.token_bytes(16)
        first = self.cron.previous(self.start) if self.pull_enabled else self.start
        self.clock = VirtualClock(min(first, self.start))
        self.gcp = GcpEmulator(self.clock, g_key)
        self.azure = AzureEmulator(self.clock, a_key, TENANT)
        self.jfrog = JFrogEmulator(self.clock, trust={GOOGLE_ISSUER: g_key, self.azure.v2_issuer: a_key,
                                                      self.azure.v1_issuer: a_key})
        self.server = EmulatorServer({"/jfrog": self.jfrog, "/gcp": self.gcp, "/azure": self.azure})
        base = self.server.start()
        host = self.server.host
        self.jfrog_url = f"{base}/jfrog"

        name = cfg["name"]
        integ = cfg["jfrog"]["integration_names"][self.cloud]
        group = cfg["jfrog"]["reader_group"]
        artifact_repo = cfg["verify"]["artifact_path"].split("/", 1)[0]
        self.jfrog.grant(group, {artifact_repo, "docker-virtual"})
        self.folder = cfg["verify"]["artifact_path"].rsplit("/", 1)[0]

        common = {"JFROG_URL": self.jfrog_url, "JFROG_OIDC_PROVIDER": integ, "JFROG_OIDC_ALLOW_HTTP": "1"}
        if cfg["jfrog"].get("project_key"):
            common["JFROG_PROJECT_KEY"] = cfg["jfrog"]["project_key"]
        if self.cloud == "gcp":
            self.jfrog.add_integration(integ, GOOGLE_ISSUER, GCP_AUDIENCE)
            self.gcp.add_account("runtime", f"{name}@{self.gcp.project_id}.iam.gserviceaccount.com", "100000000000000000001")
            self.gcp.add_account("rotator", f"jfrog-rotator@{self.gcp.project_id}.iam.gserviceaccount.com", "100000000000000000002")
            runtime_claims, pull_claims = {"sub": "100000000000000000001"}, {"sub": "100000000000000000002"}
            self.runtime_env = {**common, "JFROG_OIDC_AUDIENCE": sim.get("runtime_audience", GCP_AUDIENCE),
                                "JFROG_OIDC_SOURCE": "gcp", "GCE_METADATA_HOST": f"{host}/gcp/md/runtime"}
        else:
            version = int(sim.get("access_token_version", 2))
            self.azure.set_app(APP_ID, version=version, role_gate=bool(sim.get("app_role_gate", False)),
                               assigned={RUNTIME_ID["principal"], ROTATOR_ID["principal"]})
            self.jfrog.add_integration(integ, self.azure.v2_issuer, APP_ID)
            self.headers = {"runtime": secrets.token_hex(8), "rotator": secrets.token_hex(8)}
            self.azure.add_workload("runtime", self.headers["runtime"], {RUNTIME_ID["client"]: RUNTIME_ID["principal"]})
            self.azure.add_workload("rotator", self.headers["rotator"], {ROTATOR_ID["client"]: ROTATOR_ID["principal"]})
            runtime_claims = {"oid": RUNTIME_ID["principal"], "tid": TENANT}
            pull_claims = {"oid": ROTATOR_ID["principal"], "tid": TENANT}
            self.runtime_env = {**common, "JFROG_OIDC_AUDIENCE": sim.get("runtime_audience", f"api://{APP_ID}"),
                                "JFROG_OIDC_SOURCE": "azure", "IDENTITY_ENDPOINT": f"{base}/azure/mi/runtime",
                                "IDENTITY_HEADER": self.headers["runtime"], "AZURE_CLIENT_ID": RUNTIME_ID["client"]}

        self.mapping_names = {"runtime": f"{name}-runtime", "pull": f"{name}-pull"}
        self.jfrog.add_mapping(self.mapping_names["runtime"], integ, runtime_claims, 10, cfg["runtime"]["username"],
                               cfg["runtime"]["ttl_seconds"], [group])
        pp = cfg["pull_path"]
        if self.pull_enabled:
            self.jfrog.add_mapping(self.mapping_names["pull"], integ, pull_claims, 20, pp["registry_username"],
                                   pp["ttl_seconds"], [group])
        min_ttl = str(int(pp.get("min_ttl_seconds", 1800)))

        if self.cloud == "gcp":
            self.secret = "jfrog-upstream"
            self.gcp.add_secret(self.secret, PLACEHOLDER)
            repo = next(iter(pp.get("gcp_repositories") or {"jfrog-docker": {}}))
            region = cfg["target"]["gcp"].get("region") or "us-central1"
            self.gcp.add_remote_repo(region, repo, pp["registry_username"], self.gcp.version_name(self.secret, 1))
            self.platform = ArPlatform(self.gcp, self.jfrog_url, repo, self.sc["load"]["uncached_pull_ratio"])
            self.rotator_env = {
                **common, "ROTATOR_TARGET": "gcp", "JFROG_OIDC_AUDIENCE": GCP_AUDIENCE,
                "GCE_METADATA_HOST": f"{host}/gcp/md/rotator",
                "GCP_SECRET": f"projects/{self.gcp.project_id}/secrets/{self.secret}",
                "AR_REPOSITORIES": f"projects/{self.gcp.project_id}/locations/{region}/repositories/{repo}",
                "KEEP_VERSIONS": "2", "MIN_TTL_SECONDS": min_ttl,
            }
        else:
            self.vault, self.secret = "kv-sim", "jfrog-registry-token"
            self.azure.add_secret(self.vault, self.secret, PLACEHOLDER)
            if kind == "azure_functions_flex":
                self.platform = FlexPlatform()
            elif kind == "azure_functions_premium":
                self.platform = KvPlatform(self.azure, self.jfrog_url, self.vault, self.secret, pp["registry_username"],
                                           "Premium Functions", "scale-out")
            else:
                label = "Functions on ACA" if kind == "azure_functions_aca" else "Container Apps"
                self.platform = KvPlatform(self.azure, self.jfrog_url, self.vault, self.secret, pp["registry_username"],
                                           label, "replica start")
            self.function_app_id = (f"/subscriptions/{cfg['target']['azure'].get('subscription') or 'sub'}/resourceGroups/"
                                    f"{cfg['target']['azure'].get('resource_group') or 'rg'}/providers/Microsoft.Web/sites/{name}")
            self.rotator_env = {
                **common, "ROTATOR_TARGET": "azure", "JFROG_OIDC_AUDIENCE": f"api://{APP_ID}",
                "IDENTITY_ENDPOINT": f"{base}/azure/mi/rotator", "IDENTITY_HEADER": self.headers["rotator"],
                "AZURE_CLIENT_ID": ROTATOR_ID["client"], "KEY_VAULT_URI": f"{base}/azure/kv/{self.vault}",
                "KEY_VAULT_SECRET": self.secret, "MIN_TTL_SECONDS": min_ttl,
            }
            if kind == "azure_functions_premium" and pp.get("function_app_refresh", True):
                self.rotator_env["FUNCTION_APP_IDS"] = self.function_app_id
        self.base = base

        # Fault windows in absolute time; open-ended faults last to the end.
        self._windows = [{"type": f["type"], "params": f["params"], "start": self.start + f["start_s"],
                          "end": self.start + f["end_s"] if f["end_s"] is not None else self.end + 1}
                         for f in self.sc["faults"]]

    # ------------------------------------------------------------- secrets

    def _latest_label(self):
        if self.cloud == "gcp":
            return f"v{self.gcp.latest(self.secret)['n']}"
        return self.azure.latest(self.vault, self.secret)["label"]

    def _note_hold(self, t: float) -> None:
        held = self.platform.holds() if self.pull_enabled else None
        if held != self._last_hold:
            self._last_hold = held
            self.holds.append({"t": self._rel(t), "version": held})
            if held:
                self._emit(t, "hold", f"{self.platform.label} now pulls with {held}", version=held)

    # ------------------------------------------------------------- events

    def _rotation(self, t: float, warmup: bool = False) -> None:
        before = self._latest_label()
        refreshes_before = len(self.azure.refreshes)
        rotate.SM = f"{self.base}/gcp/sm/v1"
        rotate.AR = f"{self.base}/gcp/ar/v1"
        rotate.ARM = f"{self.base}/azure/arm"
        out = io.StringIO()
        with _workload_env(self.rotator_env), contextlib.redirect_stdout(out):
            try:
                rc = rotate.main(clock=self.clock.now, sleep=self.clock.sleep)
            except SystemExit as exc:  # require() exits before main's own handler
                print(json.dumps({"event": "rotation_failed", "error": str(exc), "severity": "ERROR"}))
                rc = 1
        lines = []
        for line in out.getvalue().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and "event" in rec:
                lines.append(rec)
        for rec in lines:
            fields = {k: v for k, v in rec.items() if k not in ("event", "severity")}
            self._emit(self.clock.now(), "rotator", f"{rec['event']} {json.dumps(fields, sort_keys=True)}".strip())

        after = self._latest_label()
        wrote = after != before
        exp = next((r.get("expires_at") for r in lines if r["event"] in ("secret_version_added", "keyvault_secret_set")), None)
        # The summary error plus any per-step failure detail (e.g. an ARM refresh 403).
        details = [r.get("error") for r in lines if r["event"].endswith("_failed") and r.get("error")]
        error = "; ".join(dict.fromkeys(details)) or None
        if wrote:
            self.cred_info[after] = {"written": t, "exp": float(exp) if exp else None}
            self.tokens.append({"version": after, "start": self._rel(t), "end": self._rel(float(exp)) if exp else None})
        ok = rc == 0
        rec = {"t": self._rel(t), "ok": ok, "version": after if wrote else None, "error": redact.text(error) if error else None,
               "faults": self._faults_at(t), "warmup": warmup}
        self.rotations.append(rec)
        if ok:
            self.last_success = t

        if isinstance(self.platform, KvPlatform):
            if self.kind == "azure_functions_premium":
                if len(self.azure.refreshes) > refreshes_before:
                    self._load_platform(self.clock.now())
            elif wrote and not warmup:
                self._push(t + self.rng.uniform(*ACA_LAG), "kv_refresh")

    def _load_platform(self, t: float) -> None:
        self.platform.load(t)
        if self.kind == "azure_functions_premium":
            self._push(t + PREMIUM_CACHE, "premium_reresolve", t)
        self._note_hold(t)

    def _new_instance(self, t: float) -> None:
        with _workload_env(self.runtime_env):
            self.instances.append((t, JFrogTokenProvider.from_env(clock=self.clock.now, sleep=self.clock.sleep)))
        lifetime = self.sc["load"]["instance_lifetime_minutes"] * 60
        # Retire instances past their lifetime, always keeping the newest one.
        self.instances = [i for i in self.instances[:-1] if t - i[0] < lifetime] + self.instances[-1:]

    def _cold_start(self, t: float) -> None:
        if self.pull_enabled and self.platform.pulls:
            res = self.platform.pull(self.rng)
            outage = (not res["ok"]) and _transient(res["status"]) and self._in_outage(t)
            info = self.cred_info.get(res["version"], {})
            rec = {"t": self._rel(t), **res, "reason": redact.text(res["reason"]), "outage": outage,
                   "cred_exp": self._rel(info["exp"]) if info.get("exp") else None}
            self.pulls.append(rec)
            if not res["ok"]:
                self._emit(t, "pull", f"{self.platform.label} {self.platform.action} failed: {res['reason']}",
                           version=res["version"], outage=outage)
                return
            if not res["cached"]:
                self._emit(t, "pull", f"{self.platform.label} {self.platform.action}: uncached pull ok with {res['version']}")
        self._new_instance(t)

    def _requests(self, t: float, n: int) -> None:
        mark = len(self._capture.messages)
        status, reason = None, ""
        instance = self.rng.choice(self.instances)[1]
        with _workload_env(self.runtime_env):
            try:
                get_json(instance, f"/artifactory/api/storage/{self.folder}")
                ok = True
            except JFrogOidcError as exc:
                ok = False
                status = getattr(exc, "status", None)
                if isinstance(exc, IdentityError):
                    status = 503 if "unreachable" in str(exc) or "HTTP 5" in str(exc) else 400
                elif isinstance(exc, ExchangeError) and status is None:
                    status = 0 if exc.retryable else 400
                reason = str(exc)
            except urllib.error.HTTPError as exc:
                with exc:
                    ok, status = False, exc.code
                    body = exc.read()[:300].decode("utf-8", "replace")
                reason = f"artifact read HTTP {status}: {body}"
            except (urllib.error.URLError, OSError) as exc:
                ok, status, reason = False, 0, f"JFrog unreachable: {exc}"
        # Served from cache: the client logged a failed refresh, or JFrog Access was
        # down so no token could have been minted for this request.
        cached = (any("serving cached token" in m for m in self._capture.messages[mark:])
                  or "access" in self.jfrog.down)
        if ok:
            outcome = "ok_cached" if cached else "ok"
        elif _transient(status) and self._in_outage(t):
            outcome = "unavailable"
        else:
            outcome = "fail"
        prev = self.requests[-1]["outcome"] if self.requests else "ok"
        self.requests.append({"t": self._rel(t), "n": n, "outcome": outcome, "reason": redact.text(reason)})
        if outcome != prev and not (outcome.startswith("ok") and prev.startswith("ok")):
            self._emit(t, "request", f"runtime requests now {outcome}" + (f": {reason}" if reason else ""))
        elif outcome == "ok_cached" and prev == "ok":
            self._emit(t, "request", "runtime serving cached JFrog token (refresh failing)")

    def _fault(self, t: float, f: dict, opening: bool) -> None:
        ftype, params = f["type"], f["params"]
        self.fault_log.append({"t": self._rel(t), "type": ftype, "open": opening})
        if ftype == "jfrog_outage":
            self.jfrog.set_down((params.get("services") or SERVICES) if opening else ())
        elif ftype == "rotator_down":
            (self.gcp if self.cloud == "gcp" else self.azure).set_identity_down("rotator", opening)
        elif ftype in ("sa_recreated", "identity_recreated") and opening:
            if self.cloud == "gcp":
                self.gcp.recreate_account("runtime", "100000000000000000099")
            else:
                self.azure.workloads["runtime"]["identities"][RUNTIME_ID["client"]] = "0b1d0000-0000-4000-8000-000000000099"
        elif ftype == "arm_refresh_forbidden":
            self.azure.refresh_forbidden = opening
        elif ftype == "mapping_revoked":
            self.jfrog.revoke_mapping(self.mapping_names[params.get("mapping", "runtime")], opening)
        elif ftype == "integration_misconfigured":
            integ = self.jfrog.integrations[self.cfg["jfrog"]["integration_names"][self.cloud]]
            if opening:
                f["_saved"] = integ["issuer"]
                integ["issuer"] = params.get("issuer", "https://wrong-issuer.example")
            elif "_saved" in f:
                integ["issuer"] = f["_saved"]
        self._emit(t, "fault", f"fault {ftype} {'starts' if opening else 'ends'}", params=params)

    def _alert_eval(self, t: float) -> None:
        if not self.pull_enabled:
            return
        after = int(self.cfg["pull_path"].get("alert_after_seconds") or 0)
        if not after:
            return
        since = self.last_success if self.last_success is not None else self.start
        firing = (t - since) > after
        if firing != self.alert_firing:
            self.alert_firing = firing
            self.alerts.append({"t": self._rel(t), "firing": firing})
            self._emit(t, "alert", (f"alert FIRING: no successful rotation for {(t - since) / 3600:.1f} h"
                                    if firing else "alert resolved"))

    # -------------------------------------------------------------- run

    def run(self) -> dict:
        wall = time.perf_counter()
        with SIM_LOCK:
            self._capture = _LogCapture()
            logger = logging.getLogger("jfrog_oidc")
            saved = (logger.level, logger.propagate)
            logger.addHandler(self._capture)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            try:
                self._build()
                self._emit(self.clock.now(), "sim", f"simulation {self.sc['name']} starts: {model.KINDS[self.kind]['label']}")
                if self.pull_enabled:
                    self._rotation(self.clock.now(), warmup=True)
                    if isinstance(self.platform, KvPlatform):
                        self._load_platform(self.clock.now())
                    self._note_hold(self.clock.now())
                self.clock.advance_to(self.start)
                self._new_instance(self.start)
                self._schedule()
                self._loop()
                self.clock.advance_to(self.end)
                self._emit(self.end, "sim", "simulation ends")
            finally:
                if getattr(self, "server", None):
                    self.server.stop()
                logger.removeHandler(self._capture)
                logger.setLevel(saved[0])
                logger.propagate = saved[1]
        return self._result(time.perf_counter() - wall)

    def _schedule(self) -> None:
        load = self.sc["load"]
        if self.pull_enabled:
            for t in self.cron.ticks(self.start, self.end):
                self._push(t, "rotation")
        rate = load["cold_starts_per_hour"] / 3600.0
        t = self.start
        while rate > 0:
            t += self.rng.expovariate(rate)
            if t >= self.end:
                break
            self._push(t, "cold_start")
        for minute in range(int((self.end - self.start) // 60)):
            n = _poisson(self.rng, load["requests_per_minute"])
            if n:
                self._push(self.start + minute * 60 + 30, "requests", n)
        for w in self._windows:
            self._push(w["start"], "fault_open", w)
            if w["end"] <= self.end:
                self._push(w["end"], "fault_close", w)
        for t in range(int(self.start), int(self.end), ALERT_EVERY):
            self._push(t, "alert_eval")

    def _loop(self) -> None:
        handlers = {
            "rotation": lambda t, d: self._rotation(t),
            "cold_start": lambda t, d: self._cold_start(t),
            "requests": lambda t, d: self._requests(t, d),
            "fault_open": lambda t, d: self._fault(t, d, True),
            "fault_close": lambda t, d: self._fault(t, d, False),
            "alert_eval": lambda t, d: self._alert_eval(t),
            "kv_refresh": lambda t, d: self._load_platform(t),
            "premium_reresolve": lambda t, d: self._load_platform(t) if self.platform.loaded_at == d else None,
        }
        while self._queue:
            t, _, kind, data = heapq.heappop(self._queue)
            if t >= self.end:
                break
            self.clock.advance_to(t)
            # A retry sleep may have moved the clock past t: record when it really happened.
            handlers[kind](max(t, self.clock.now()), data)
            if self.pull_enabled:
                self._note_hold(self.clock.now())

    # ----------------------------------------------------------- verdict

    def _failures(self):
        fails = []
        for p in self.pulls:
            if not p["ok"]:
                fails.append({"t": p["t"], "kind": "pull", "outage": p["outage"], "rec": p})
        for r in self.requests:
            if r["outcome"] in ("fail", "unavailable"):
                fails.append({"t": r["t"], "kind": "request", "outage": r["outcome"] == "unavailable", "rec": r})
        return sorted(fails, key=lambda f: f["t"])

    def _exposures(self) -> list:
        """Intervals when the platform held an expired credential or the placeholder.

        Any pull landing in one fails, whether or not this run's random cold
        starts happened to hit it, so they count against the verdict.
        """
        if not (self.pull_enabled and self.platform.pulls):
            return []
        out = []
        duration = self.sc["duration_s"]
        for i, h in enumerate(self.holds):
            s = max(h["t"], 0.0)
            e = self.holds[i + 1]["t"] if i + 1 < len(self.holds) else duration
            e = min(e, duration)
            if e <= s or not h["version"]:
                continue
            info = self.cred_info.get(h["version"])
            if info is None:
                bad_from = s  # bootstrap placeholder
            elif info["exp"] is None:
                continue
            else:
                bad_from = max(s, self._rel(info["exp"]))
            if bad_from < e:
                out.append({"start": bad_from, "end": e, "version": h["version"], "placeholder": info is None})
        return out

    def _failed_runs_text(self, since_rel: float, until_rel: float) -> str:
        runs = [r for r in self.rotations if not r["ok"] and since_rel <= r["t"] <= until_rel]
        if not runs:
            return ""
        times = [self.hhmm(self.start + r["t"]) for r in runs]
        if len(times) > 6:
            times = times[:3] + ["…"] + times[-2:]
        faults = sorted({f for r in runs for f in r["faults"]})
        why = f"fault {', '.join(faults)}" if faults else (runs[0]["error"] or "error")
        noun = "rotation" if len(runs) == 1 else "rotations"
        return f"{noun} {', '.join(times)} failed ({why})"

    def _chain(self, first: dict) -> list:
        chain = []
        t_abs = self.start + first["t"]
        for f in self.fault_log:
            if f["open"] and f["t"] <= first["t"] and f["type"] in ("sa_recreated", "identity_recreated",
                                                                    "mapping_revoked", "integration_misconfigured"):
                desc = {"sa_recreated": "service account recreated (new uniqueId)",
                        "identity_recreated": "runtime identity recreated (new ID)",
                        "mapping_revoked": "identity mapping revoked",
                        "integration_misconfigured": "JFrog integration issuer changed"}[f["type"]]
                chain.append(f"{desc} at {self.hhmm(self.start + f['t'])}")
        rec = first["rec"]
        if first["kind"] in ("pull", "exposure"):
            label = rec["version"]
            info = self.cred_info.get(label)
            since = self._rel(info["written"]) if info else -86400.0
            failed = self._failed_runs_text(since + 1, first["t"])
            if failed:
                chain.append(failed)
            if info is None:
                chain.append(f"{self.platform.label} still holds the bootstrap placeholder ({label}): no rotation has succeeded")
            elif info["exp"] is not None and info["exp"] <= t_abs:
                chain.append(f"token {label} expired {self.hhmm(info['exp'])}")
            newer = [v for v, i in self.cred_info.items() if i["written"] <= t_abs and (info is None or i["written"] > info["written"])]
            if newer:
                n = newer[-1]
                lag = ("Key Vault reference cache, up to 24 h without ARM refresh" if self.kind == "azure_functions_premium"
                       else "secret refresh lag, 5–30 min")
                chain.append(f"{n} was written {self.hhmm(self.cred_info[n]['written'])} but {self.platform.label} "
                             f"had not picked it up ({lag})")
            if first["kind"] == "pull":
                chain.append(f"{self.platform.label} {self.platform.action} {self.hhmm(t_abs)} failed: {rec['reason']}")
            else:
                chain.append(f"{self.platform.label} held an unusable credential {self.hhmm(t_abs)}–"
                             f"{self.hhmm(self.start + rec['end'])}: any {self.platform.action} in that window fails "
                             f"(none landed in it in this run)")
        else:
            failed = self._failed_runs_text(-86400, first["t"])
            if failed and "MIN_TTL" in failed:
                chain.append(failed)
            chain.append(f"runtime request {self.hhmm(t_abs)} failed: {rec['reason']}")
        return chain

    def _root_cause(self, chain: list):
        rot_errors = " ".join(r["error"] or "" for r in self.rotations)
        text = " ".join(chain) + " " + rot_errors
        pp = self.cfg["pull_path"]
        math_ = model.ttl_math(self.cfg) if self.pull_enabled else {}
        if "MIN_TTL_SECONDS" in text:
            return (f"The pull mapping issues {pp['ttl_seconds']}s tokens but the rotator requires "
                    f"MIN_TTL_SECONDS={pp.get('min_ttl_seconds', 1800)}: every rotation is refused, so the platform keeps "
                    f"the bootstrap placeholder or an expiring token. Raise pull_path.ttl_seconds to at least "
                    f"{max(int(pp.get('min_ttl_seconds', 1800)), math_.get('min_safe_ttl_seconds', 0))}s.")
        if "sts.windows.net" in text:
            return ("The audience app registration issues v1 tokens (iss https://sts.windows.net/<tenant>/, aud api://…), "
                    "but the JFrog integration expects the v2 issuer and the app ID as audience. Set "
                    "requestedAccessTokenVersion = 2 on the app registration (api.requested_access_token_version in Terraform).")
        if "AADSTS501051" in text:
            return ("The app-role gate is on and this managed identity has no app role on the audience app: add it to "
                    "audience_assignees.")
        if "audience mismatch" in text:
            return ("JFROG_OIDC_AUDIENCE on the runtime does not match the JFrog integration's audience; every exchange "
                    "is rejected. Fix the env var (the wire step sets it).")
        if "no identity mapping matched" in text:
            return ("The identity's claims no longer match any JFrog identity mapping (for example the service account "
                    "was recreated and has a new uniqueId). Re-run Discover and re-apply so the mapping uses the current ID.")
        if "refresh" in rot_errors and "403" in rot_errors:
            return ("The rotator cannot refresh Key Vault references on the function app (needs "
                    "Microsoft.Web/sites/config/configreferences/appsettings/refresh/action, e.g. Website Contributor). "
                    "Premium Functions then cache the reference for up to 24 h and pull with an expired token.")
        if any("rotator_down" in r["faults"] for r in self.rotations) or "fault rotator_down" in text:
            return (f"The rotator missed consecutive runs. This config survives {math_.get('survives_missed_rotations')} "
                    f"missed rotation(s): TTL {math_.get('pull_ttl_seconds')}s, interval "
                    f"{math_.get('rotation_interval_seconds')}s, pickup lag {math_.get('pickup_lag_seconds') or 0}s. "
                    "Raise the pull TTL, and act on the stale-rotation alert.")
        if "expired" in text and math_ and math_.get("survives_missed_rotations", 1) < 1:
            return (f"The pull TTL ({math_['pull_ttl_seconds']}s) does not cover one rotation interval "
                    f"({math_['rotation_interval_seconds']}s) plus the platform's pickup lag "
                    f"({math_['pickup_lag_seconds'] or 0}s), so tokens expire before the platform uses the next one. "
                    f"Use at least {math_['min_safe_ttl_seconds']}s or rotate more often.")
        if "unavailable" in text:
            return "JFrog was unavailable; failures are confined to the outage window."
        return None

    def _result(self, elapsed: float) -> dict:
        fails = self._failures()
        hard = [f for f in fails if not f["outage"]]
        exposures = self._exposures()
        # An exposure with no observed failure in it still fails the verdict.
        unobserved = [x for x in exposures
                      if not any(f["kind"] == "pull" and x["start"] <= f["t"] < x["end"] for f in hard)]
        if unobserved:
            hard = sorted(hard + [{"t": x["start"], "kind": "exposure", "outage": False, "rec": x} for x in unobserved],
                          key=lambda f: f["t"])
        if not fails and not hard:
            verdict, chain, root = "pass", [], None
        else:
            verdict = "fail" if hard else "degraded"
            first = hard[0] if hard else fails[0]
            chain = self._chain(first)
            root = self._root_cause(chain)
        weight = lambda outcome: sum(r["n"] for r in self.requests if r["outcome"] == outcome)  # noqa: E731
        first_alert = next((a["t"] for a in self.alerts if a["firing"]), None)
        summary = {
            "rotations_ok": sum(1 for r in self.rotations if r["ok"] and not r["warmup"]),
            "rotations_failed": sum(1 for r in self.rotations if not r["ok"] and not r["warmup"]),
            "pulls_ok": sum(1 for p in self.pulls if p["ok"]),
            "pulls_cached": sum(1 for p in self.pulls if p["ok"] and p["cached"]),
            "pulls_failed": sum(1 for p in self.pulls if not p["ok"]),
            "requests_ok": weight("ok"),
            "requests_ok_cached": weight("ok_cached"),
            "requests_failed": weight("fail"),
            "requests_unavailable": weight("unavailable"),
            "alert_first_fired_s": first_alert,
            "first_failure_s": (hard[0]["t"] if hard else (fails[0]["t"] if fails else None)),
            "exposure_s": round(sum(x["end"] - x["start"] for x in exposures), 1),
            "tokens_minted": len(self.jfrog.minted),
            "emulator_requests": self.server.stats.requests,
            "elapsed_s": round(elapsed, 3),
        }
        return {
            "scenario": self.sc["name"],
            "description": self.sc.get("description", ""),
            "target_kind": self.kind,
            "target_label": model.KINDS[self.kind]["label"],
            "platform_label": getattr(self.platform, "label", ""),
            "start": self.sc["clock_start"],
            "start_epoch": self.start,
            "duration_s": self.sc["duration_s"],
            "seed": self.sc["seed"],
            "load": self.sc["load"],
            "verdict": verdict,
            "chain": chain,
            "root_cause": root,
            "ttl_math": model.ttl_math(self.cfg) if self.pull_enabled else {},
            "summary": summary,
            "metrics": {
                "rotations": self.rotations,
                "tokens": self.tokens,
                "holds": self.holds,
                "pulls": self.pulls,
                "requests": self.requests,
                "alerts": self.alerts,
                "exposures": exposures,
                "faults": [{"type": w["type"], "start": self._rel(w["start"]), "end": self._rel(min(w["end"], self.end))}
                           for w in self._windows],
            },
            "events": self.events,
            "config": redact.obj({k: v for k, v in self.cfg.items() if k != "sim"}),
        }
