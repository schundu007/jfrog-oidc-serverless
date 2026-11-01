"""Discrete-event simulation of one target's token lifecycle (spec §7.3).

The engine runs the *real* jfrog_oidc client, unmodified apart from clock/sleep
injection, against in-process emulators on a virtual clock. Event sources:
platform cold starts (Poisson), runtime requests and fault windows.

Modelling choices (read these before trusting a verdict):
- Runtime requests are bucketed per minute: the newest warm instance makes one
  real call (token() + an Artifactory storage read) per minute that has any
  requests, and the outcome is weighted by the number of requests that minute.
- Requests are spread over the warm instances (seeded random choice). Each
  cold start adds an instance that lives load.instance_lifetime_minutes, so
  instances that started before an outage keep serving their cached token
  while new ones cannot get one.
- Runtime path only on every cloud: images reach the platform's own registry
  at build time (pipeline OIDC to JFrog), so no registry credential exists.

The process environment is shared, so
only one simulation runs at a time per process (SIM_LOCK).
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import logging
import math
import os
import random
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path

from .. import model, redact
from .clock import VirtualClock
from .emulators.aws import AwsEmulator
from .emulators.azure import AzureEmulator
from .emulators.gcp import GOOGLE_ISSUER, GcpEmulator
from .emulators.jfrog import SERVICES, JFrogEmulator
from .emulators.k8s import K8sEmulator
from .emulators.server import EmulatorServer

SIM_LOCK = threading.Lock()
ROOT = Path(__file__).resolve().parents[2]

GCP_AUDIENCE = "jfrog-gcp"
AWS_AUDIENCE = "jfrog-aws"        # tfgen.AWS_AUDIENCE
K8S_AUDIENCE = "jfrog-k8s"        # tfgen.K8S_AUDIENCE
TENANT = "72f988bf-0000-4000-8000-000000000001"
APP_ID = "5d1c2b3a-0000-4000-8000-00000000a0d1"
RUNTIME_ID = {"client": "c0ffee00-0000-4000-8000-000000000011", "principal": "0b1d0000-0000-4000-8000-000000000011"}

# Every variable the client reads. Cleared and restored around each
# call so the operator's own environment never leaks into a simulation.
_ENV_KEYS = (
    "JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SOURCE", "JFROG_OIDC_TOKEN_FILE",
    "JFROG_OIDC_ALLOW_HTTP", "JFROG_PROJECT_KEY", "GCE_METADATA_HOST", "IDENTITY_ENDPOINT", "IDENTITY_HEADER",
    "MSI_ENDPOINT", "AZURE_CLIENT_ID", "AZURE_FEDERATED_TOKEN_FILE", "K_SERVICE", "CLOUD_RUN_JOB", "FUNCTION_TARGET",
    "JFROG_OIDC_AWS_REGION", "AWS_REGION", "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_STS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_LAMBDA_FUNCTION_NAME", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "AWS_PROFILE",
    "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
    "all_proxy", "NO_PROXY", "no_proxy",
)


def _ensure_import_paths():
    p = ROOT / "client" / "src"
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


_ensure_import_paths()
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

        self.events: list = []
        self.requests: list = []
        self.fault_log: list = []   # {t, type, open}
        self.instances: list = []
        self._queue: list = []
        self._seq = itertools.count()

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
        g_key, a_key, w_key, k_key = (secrets.token_bytes(16) for _ in range(4))
        self.clock = VirtualClock(self.start)
        self.gcp = GcpEmulator(self.clock, g_key)
        self.azure = AzureEmulator(self.clock, a_key, TENANT)
        self.aws = AwsEmulator(self.clock, w_key, region=cfg["target"].get("aws", {}).get("region") or "us-east-1")
        self._tmp = tempfile.mkdtemp(prefix="jfrog-sim-")
        self.k8s = K8sEmulator(self.clock, k_key, os.path.join(self._tmp, "token"))
        self.jfrog = JFrogEmulator(self.clock, trust={GOOGLE_ISSUER: g_key, self.azure.v2_issuer: a_key,
                                                      self.azure.v1_issuer: a_key, self.aws.issuer: w_key,
                                                      self.k8s.issuer: k_key})
        self.server = EmulatorServer({"/jfrog": self.jfrog, "/gcp": self.gcp, "/azure": self.azure, "/aws": self.aws})
        base = self.server.start()
        host = self.server.host
        self.aws.host = host
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
            runtime_claims = {"sub": "100000000000000000001"}
            self.runtime_env = {**common, "JFROG_OIDC_AUDIENCE": sim.get("runtime_audience", GCP_AUDIENCE),
                                "JFROG_OIDC_SOURCE": "gcp", "GCE_METADATA_HOST": f"{host}/gcp/md/runtime"}
        elif self.cloud == "aws":
            runtime_claims = self._build_aws(cfg, common, sim, base)
        elif self.cloud == "openshift":
            runtime_claims = self._build_openshift(cfg, common, sim)
        else:
            version = int(sim.get("access_token_version", 2))
            self.azure.set_app(APP_ID, version=version, role_gate=bool(sim.get("app_role_gate", False)),
                               assigned={RUNTIME_ID["principal"]})
            self.jfrog.add_integration(integ, self.azure.v2_issuer, APP_ID)
            self.headers = {"runtime": secrets.token_hex(8)}
            self.azure.add_workload("runtime", self.headers["runtime"], {RUNTIME_ID["client"]: RUNTIME_ID["principal"]})
            runtime_claims = {"oid": RUNTIME_ID["principal"], "tid": TENANT}
            self.runtime_env = {**common, "JFROG_OIDC_AUDIENCE": sim.get("runtime_audience", f"api://{APP_ID}"),
                                "JFROG_OIDC_SOURCE": "azure", "IDENTITY_ENDPOINT": f"{base}/azure/mi/runtime",
                                "IDENTITY_HEADER": self.headers["runtime"], "AZURE_CLIENT_ID": RUNTIME_ID["client"]}

        self.mapping_names = {"runtime": f"{name}-runtime"}
        self.jfrog.add_mapping(self.mapping_names["runtime"], integ, runtime_claims, 10, cfg["runtime"]["username"],
                               cfg["runtime"]["ttl_seconds"], [group])

        # Fault windows in absolute time; open-ended faults last to the end.
        self._windows = [{"type": f["type"], "params": f["params"], "start": self.start + f["start_s"],
                          "end": self.start + f["end_s"] if f["end_s"] is not None else self.end + 1}
                         for f in self.sc["faults"]]

    def _build_aws(self, cfg: dict, common: dict, sim: dict, base: str) -> dict:
        """Lambda gets its role's keys as env vars; ECS tasks and App Runner read the
        container credentials endpoint (FULL_URI + authorization token)."""
        role = cfg["target"].get("discovered", {}).get("dedicated_identity") or f"{cfg['name']}-role"
        integ = cfg["jfrog"]["integration_names"]["aws"]
        self.jfrog.add_integration(integ, self.aws.issuer, AWS_AUDIENCE)
        keys = self.aws.add_workload("runtime", role)
        env = {**common, "JFROG_OIDC_AUDIENCE": sim.get("runtime_audience", AWS_AUDIENCE), "JFROG_OIDC_SOURCE": "aws",
               "JFROG_OIDC_AWS_REGION": self.aws.region, "AWS_ENDPOINT_URL_STS": f"{base}/aws/sts"}
        if self.kind == "aws_lambda":
            env.update(AWS_LAMBDA_FUNCTION_NAME=cfg["name"], AWS_REGION=self.aws.region,
                       AWS_ACCESS_KEY_ID=keys["AccessKeyId"], AWS_SECRET_ACCESS_KEY=keys["SecretAccessKey"],
                       AWS_SESSION_TOKEN=keys["Token"])
        else:
            env.update(AWS_CONTAINER_CREDENTIALS_FULL_URI=f"{base}/aws/creds/runtime",
                       AWS_CONTAINER_AUTHORIZATION_TOKEN=self.aws.workloads["runtime"]["auth"])
        self.runtime_env = env
        return {"sub": self.aws.workloads["runtime"]["role"]}

    def _build_openshift(self, cfg: dict, common: dict, sim: dict) -> dict:
        """The projected service-account token file, refreshed by the emulated kubelet."""
        t = cfg["target"]["openshift"]
        ns = t.get("namespace") or "default"
        sa = cfg["target"].get("discovered", {}).get("dedicated_identity") or f"{cfg['name']}-sa"
        integ = cfg["jfrog"]["integration_names"]["openshift"]
        self.jfrog.add_integration(integ, self.k8s.issuer, K8S_AUDIENCE)
        # The projected volume's audience is what the token carries; the env var is not used by the file source.
        self.k8s.project(ns, sa, sim.get("runtime_audience", K8S_AUDIENCE), 3600)
        self.runtime_env = {**common, "JFROG_OIDC_AUDIENCE": K8S_AUDIENCE, "JFROG_OIDC_SOURCE": "file",
                            "JFROG_OIDC_TOKEN_FILE": self.k8s.path}
        return {"sub": f"system:serviceaccount:{ns}:{sa}"}

    # ------------------------------------------------------------- events

    def _new_instance(self, t: float) -> None:
        with _workload_env(self.runtime_env):
            self.instances.append((t, JFrogTokenProvider.from_env(clock=self.clock.now, sleep=self.clock.sleep)))
        lifetime = self.sc["load"]["instance_lifetime_minutes"] * 60
        # Retire instances past their lifetime, always keeping the newest one.
        self.instances = [i for i in self.instances[:-1] if t - i[0] < lifetime] + self.instances[-1:]

    def _cold_start(self, t: float) -> None:
        self._new_instance(t)

    def _requests(self, t: float, n: int) -> None:
        if self.cloud == "openshift":
            self.k8s.tick()
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
        elif ftype == "federation_disabled":
            self.aws.federation_enabled = not opening
        elif ftype in ("sa_recreated", "identity_recreated") and opening:
            if self.cloud == "aws":
                self.aws.recreate_role("runtime")         # same ARN: the sub mapping still matches
            elif self.cloud == "openshift":
                self.k8s.recreate_service_account()       # same name: the sub mapping still matches
            elif self.cloud == "gcp":
                self.gcp.recreate_account("runtime", "100000000000000000099")
            else:
                self.azure.workloads["runtime"]["identities"][RUNTIME_ID["client"]] = "0b1d0000-0000-4000-8000-000000000099"
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
                self._new_instance(self.start)
                self._schedule()
                self._loop()
                self.clock.advance_to(self.end)
                self._emit(self.end, "sim", "simulation ends")
            finally:
                if getattr(self, "server", None):
                    self.server.stop()
                if getattr(self, "_tmp", None):
                    shutil.rmtree(self._tmp, ignore_errors=True)
                logger.removeHandler(self._capture)
                logger.setLevel(saved[0])
                logger.propagate = saved[1]
        return self._result(time.perf_counter() - wall)

    def _schedule(self) -> None:
        load = self.sc["load"]
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

    def _loop(self) -> None:
        handlers = {
            "cold_start": lambda t, d: self._cold_start(t),
            "requests": lambda t, d: self._requests(t, d),
            "fault_open": lambda t, d: self._fault(t, d, True),
            "fault_close": lambda t, d: self._fault(t, d, False),
        }
        while self._queue:
            t, _, kind, data = heapq.heappop(self._queue)
            if t >= self.end:
                break
            self.clock.advance_to(t)
            # A retry sleep may have moved the clock past t: record when it really happened.
            handlers[kind](max(t, self.clock.now()), data)

    # ----------------------------------------------------------- verdict

    def _failures(self):
        fails = []
        for r in self.requests:
            if r["outcome"] in ("fail", "unavailable"):
                fails.append({"t": r["t"], "kind": "request", "outage": r["outcome"] == "unavailable", "rec": r})
        return sorted(fails, key=lambda f: f["t"])

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
        chain.append(f"runtime request {self.hhmm(t_abs)} failed: {rec['reason']}")
        return chain

    def _root_cause(self, chain: list):
        text = " ".join(chain)
        if "OutboundWebIdentityFederationDisabled" in text:
            region = self.cfg["target"].get("aws", {}).get("region") or "<region>"
            return ("Outbound identity federation is disabled for the AWS account, so STS GetWebIdentityToken refuses "
                    "every call and the runtime cannot get a JWT. Run once per account: "
                    f"aws iam enable-outbound-web-identity-federation --region {region}")
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
        if "unavailable" in text:
            return "JFrog was unavailable; failures are confined to the outage window."
        return None

    def _result(self, elapsed: float) -> dict:
        fails = self._failures()
        hard = [f for f in fails if not f["outage"]]
        if not fails and not hard:
            verdict, chain, root = "pass", [], None
        else:
            verdict = "fail" if hard else "degraded"
            first = hard[0] if hard else fails[0]
            chain = self._chain(first)
            root = self._root_cause(chain)
        weight = lambda outcome: sum(r["n"] for r in self.requests if r["outcome"] == outcome)  # noqa: E731
        summary = {
            "requests_ok": weight("ok"),
            "requests_ok_cached": weight("ok_cached"),
            "requests_failed": weight("fail"),
            "requests_unavailable": weight("unavailable"),
            "first_failure_s": (hard[0]["t"] if hard else (fails[0]["t"] if fails else None)),
            "tokens_minted": len(self.jfrog.minted),
            "emulator_requests": self.server.stats.requests,
            "elapsed_s": round(elapsed, 3),
        }
        return {
            "scenario": self.sc["name"],
            "description": self.sc.get("description", ""),
            "target_kind": self.kind,
            "target_label": model.KINDS[self.kind]["label"],
            "start": self.sc["clock_start"],
            "start_epoch": self.start,
            "duration_s": self.sc["duration_s"],
            "seed": self.sc["seed"],
            "load": self.sc["load"],
            "verdict": verdict,
            "chain": chain,
            "root_cause": root,
            "summary": summary,
            "metrics": {
                "requests": self.requests,
                "faults": [{"type": w["type"], "start": self._rel(w["start"]), "end": self._rel(min(w["end"], self.end))}
                           for w in self._windows],
            },
            "events": self.events,
            "config": redact.obj({k: v for k, v in self.cfg.items() if k != "sim"}),
        }
