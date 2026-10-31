"""Runs: prepare → approve → queue → poll → logs → parse (spec §5).

A run is one ADO pipeline invocation, one JFrog write, or one direct plan
(direct.py: AZURE.md Option B with az, kubectl, helm and the Access API). prepare builds the
canonical request {pipeline, branch, parameters, values_sha256}, applies the
guards and computes request_sha256. Nothing is queued until approve() is called
with that same hash; a state-changing request also needs the operator's
explicit approval, and a destructive one the cluster name typed in. Dry run is
the default wherever the scenario supports it.
"""

from __future__ import annotations

import re
import secrets
import threading
import time

from . import direct, knowledge, logic, model, redact
from .ado import Ado, AdoError, param_args
from .jfrog import Jfrog, OperatorToken
from .parse import events, summary

PILOT_EXTRAS = {"TEST_POD_IMAGE", "TEST_POD_NAME", "TEST_POD_SA", "DiagnosticImage", "ChartRepo"}
DIRECT_PLANS = ("setup", "verify", "teardown")
JFROG_ACTIONS = {"create-provider", "create-mapping", "promote", "delete-mapping", "delete-provider"}
POLL_LIMIT_SECONDS = 3600


class RunError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)


def _scenario(name):
    s = knowledge.scenario(name)
    if not s:
        raise RunError(400, f"unknown scenario {name!r}")
    return s


def prd_blocked(cfg: dict, status: dict) -> str | None:
    if cfg["cluster"].get("environment") != "PRD" and (cfg.get("template") or {}).get("Environment") != "PRD":
        return None
    rows = (status or {}).get("security") or {}
    open_rows = [r["id"] for r in knowledge.SECURITY_REVIEW["rows"] if r.get("prd_gate")
                 and (rows.get(r["id"]) or {}).get("status") != "decided"]
    return (f"{knowledge.SECURITY_REVIEW['prd_gate']} Still open: {', '.join(open_rows)}.") if open_rows else None


def build_request(cfg: dict, kind: str, name: str, params: dict, status: dict | None = None, root=".kcp-console") -> dict:
    """The canonical request and its guard result. kind: pilot | template | mappings | jfrog | direct.
    root is where a direct plan keeps its kubeconfig, values and manifests."""
    cfg = model.merged(cfg)
    params = dict(params or {})
    guards, warnings = [], []
    a = cfg["ado"]
    if (b := prd_blocked(cfg, status)):
        guards.append(b)
    if kind == "pilot":
        s = _scenario(name)
        bad = set(params) - PILOT_EXTRAS - {"DryRun"}
        if bad:
            raise RunError(400, f"parameters not allowed for {name}: {', '.join(sorted(bad))}")
        p = {"TestScenario": name}
        if s["dry_run"]:
            p["DryRun"] = bool(params.pop("DryRun", True))
        else:
            params.pop("DryRun", None)
        p.update({k: str(v) for k, v in params.items() if v not in (None, "")})
        if cfg.get("demo_mode") and s.get("restarts_daemonset"):
            guards.append(f"Demo mode: {name} restarts the DaemonSet, and the setup step rewrites the node config and "
                          f"restarts kubelet. Turn demo mode off to run it.")
        if name == "INSTALL_JFROG_PROVIDER":
            errs = model.errors(model.validate(cfg))
            if errs:
                guards.append("The values file has errors: " + "; ".join(f"{e['field'].split('.')[-1]}: {e['message']}" for e in errs))
        if p.get("ChartRepo") and p["ChartRepo"] != logic.APPROVED_CHART_REPO:
            guards.append(f"ChartRepo {p['ChartRepo']} is not {logic.APPROVED_CHART_REPO}")
        if name == "CREATE_CONTROL_POD":
            p.setdefault("TEST_POD_SA", "default")
            p.setdefault("TEST_POD_NAME", "control-pull-test")
        req = {"pipeline": a["pilot_pipeline"], "branch": a["branch"], "parameters": p,
               "values_sha256": model.values_sha256(cfg)}
        mutates, destructive = s["mutates"], s["destructive"]
        if s.get("restarts_daemonset"):
            warnings.append(f"{name} restarts the DaemonSet (the setup step rewrites the node file and restarts kubelet)")
    elif kind in ("template", "mappings"):
        if kind == "template":
            act = next((x for x in knowledge.TEMPLATE_ACTIONS if x["name"] == name), None)
            if not act:
                raise RunError(400, f"unknown action {name!r}")
            t = model.template_defaults_for(cfg)
            p = {"Action": name, "DryRun": bool(params.get("DryRun", True)),
                 **{k: str(v) for k, v in t.items() if v not in (None, "")}}
            errs = model.errors(model.validate_template(cfg))
            if errs:
                guards.append("Template inputs have errors: " + "; ".join(f"{e['field'].split('.')[-1]}: {e['message']}" for e in errs))
            pipeline, mutates, destructive = a["cluster_pipeline"], act["mutates"], act["destructive"]
        else:
            p = {"ClusterName": cfg["cluster"]["name"], "DryRun": bool(params.get("DryRun", True)),
                 "Prune": bool(params.get("Prune", False))}
            pipeline, mutates, destructive = a["mappings_pipeline"], True, bool(p["Prune"])
        try:
            param_args(p)
        except AdoError as exc:
            raise RunError(400, str(exc)) from None
        req = {"pipeline": pipeline, "branch": a["branch"], "parameters": p, "values_sha256": model.values_sha256(cfg)}
    elif kind == "jfrog":
        if name not in JFROG_ACTIONS:
            raise RunError(400, f"unknown JFrog action {name!r}")
        body = params.get("body")
        provider = params.get("provider") or model.provider_name(cfg)
        if provider in cfg["jfrog"]["protected_providers"]:
            guards.append(f"{provider} is protected: another cluster's provider. The console never edits or deletes it.")
        if name == "create-provider":
            body = model.provider_request(cfg)
        elif name == "create-mapping":
            body = model.mapping_request(cfg)
        elif name == "promote":
            src = params.get("source") or cfg["jfrog"]["promote_source_repo"]
            if not src:
                guards.append("jfrog.promote_source_repo is empty: set the repo to promote the test image from")
            body = {"targetRepo": cfg["jfrog"]["test_repo"], "dockerRepository": params.get("dockerRepository", "mcp/sonarqube"),
                    "tag": params.get("tag", "1.26-v1"), "copy": True}
            provider = src
        elif name == "delete-mapping":
            body = {"name": params.get("mapping") or cfg["jfrog"]["mapping_name"]}
        req = {"pipeline": "jfrog", "branch": "", "parameters": {"action": name, "target": provider, "body": body},
               "values_sha256": ""}
        mutates, destructive = True, name.startswith("delete")
    elif kind == "direct":
        if name not in DIRECT_PLANS:
            raise RunError(400, f"unknown direct plan {name!r}")
        dry = bool(params.get("DryRun", True))
        guards += direct.problems(cfg, name)
        if name != "verify" and cfg["values"]["provider_name"] in cfg["jfrog"]["protected_providers"]:
            guards.append(f"{cfg['values']['provider_name']} is protected: another cluster's provider. The console never edits or deletes it.")
        if name == "setup":
            # The issuer is read by the plan's first step, so a blank one is not an error here.
            errs = [e for e in model.errors(model.validate(cfg)) if e["field"] not in ("cluster.oidc_issuer", "values.provider_name")]
            if errs:
                guards.append("The configuration has errors: " + "; ".join(f"{e['field'].split('.')[-1]}: {e['message']}" for e in errs))
            if cfg.get("demo_mode") and not dry:
                guards.append("Demo mode: setup runs helm upgrade, which restarts the DaemonSet. Turn demo mode off to run it.")
        steps = direct.plan(cfg, root, name, dry)
        req = {"pipeline": "direct", "branch": "", "parameters": {"plan": name, "DryRun": dry, "steps": steps},
               "values_sha256": model.values_sha256(cfg) if name == "setup" else ""}
        mutates, destructive = True, name == "teardown"
        if name == "teardown":
            warnings.append("Teardown removes the release, the node label, the identity mapping and the OIDC provider. "
                            "Uninstall leaves the node's provider entry in place (untested cleanup).")
    else:
        raise RunError(400, f"unknown run kind {kind!r}")
    dry = req["parameters"].get("DryRun") is True
    return {"kind": kind, "name": name, "request": req,
            "request_sha256": logic.request_hash(req["pipeline"], req["branch"], req["parameters"], req["values_sha256"]),
            "changes_state": bool(mutates and not dry), "destructive": bool(destructive and not dry),
            "needs_typed_cluster": bool(destructive and not dry), "guards": guards, "warnings": warnings,
            "cluster": cfg["cluster"]["name"], "config": cfg.get("name")}


# ---------------------------------------------------------------- evidence from logs

ASSIGNED = re.compile(r"Successfully assigned (?P<ns>[\w-]+)/(?P<pod>[\w.-]+) to (?P<node>[\w.-]+)")
TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def pod_from_log(text: str, run: dict, cfg: dict) -> dict | None:
    m = ASSIGNED.search(text or "")
    if not m:
        return None
    ev = events.parse_pod_events(text)
    p = run["request"]["parameters"]
    image = (ev["pulled"] or ev["already_present"] or {}).get("image") or (ev["pulling"] or [None])[0] or p.get("TEST_POD_IMAGE") or cfg["jfrog"]["test_image"]
    line = (ev["pulled"] or ev["already_present"] or {}).get("line", "")
    ts = next((TS.match(l).group(1) + "Z" for l in (text or "").splitlines() if TS.match(l) and ("Pulled" in l or "Failed" in l)), None)
    return {"name": m.group("pod"), "node": m.group("node"), "image": image, "kind": "control" if run["name"] == "CREATE_CONTROL_POD" else "test",
            "sa": p.get("TEST_POD_SA") or cfg["k8s"]["service_account"], "events": "\n".join(
                l for l in summary.clean(text) if any(w in l for w in ("Normal", "Warning", "Events:", "phase", "Forbidden")))
            or text, "time": ts or run.get("finished") or "", "run": run["id"], "line": line}


def bundle(cfg: dict, runs: list, logs: dict, status: dict) -> dict:
    """The evidence bundle diagnose() reads, from this config's completed runs (oldest first)."""
    cfg = model.merged(cfg)
    b = {"config": cfg, "values_file": model.render_values(cfg), "helm_values": "", "manifest": "", "kubelet_checks": [],
         "pods": [], "pod_config": "", "install_logs": [], "reset_logs": [], "mirror": None, "probes": [], "promotes": [],
         "remote": None, "audit": (status or {}).get("audit") or [], "daemonsets": [], "runs": []}
    facts = (status or {}).get("discover") or {}
    b["helm_values"] = facts.get("helm_values") or ""
    b["manifest"] = facts.get("manifest") or ""
    b["daemonsets"] = [dict(d, age_days="?", deleted=False) for d in facts.get("daemonsets") or [] if d.get("jfrog") and not d.get("release")]
    for r in sorted(runs, key=lambda r: r.get("created", "")):
        if r.get("config") != cfg["name"] or r.get("status") != "succeeded" and r.get("status") != "failed":
            continue
        text = logs.get(r["id"], "")
        n = r["name"]
        b["runs"].append(r["id"])
        if n == "CHECK_KUBELET_PROVIDER" or (r["kind"] == "template" and n in ("Verify", "Preflight")):
            if "QUICK SUMMARY" in text or "Node inspected" in text:
                b["kubelet_checks"].append(text)
        if n in ("CREATE_TEST_POD", "CREATE_CONTROL_POD") or (r["kind"] == "template" and n == "Verify"):
            pod = pod_from_log(text, r, cfg)
            if pod:
                b["pods"].append(pod)
        if n in ("INSTALL_JFROG_PROVIDER", "Install"):
            b["install_logs"].append(text)
        if n in ("RESET_NODE_JFROG_ENTRY", "ResetNodeEntry"):
            b["reset_logs"].append(text)
        if n == "CHECK_JFROG_POD_CONFIG":
            sec = summary.pod_config_sections(text)
            b["pod_config"] = text
            if sec["helm_values"]:
                b["helm_values"] = sec["helm_values"]
        if n == "MIRROR_JFROG_BINARY":
            m = re.search(r"downloaded (\d+) bytes", text)
            if m:
                b["mirror"] = {"size": int(m.group(1)), "log": text}
        if r["kind"] == "jfrog" and r["name"] == "promote":
            res = r.get("result") or {}
            b["promotes"].append({"source": r["request"]["parameters"]["target"], "status": res.get("status"), "body": res.get("body", "")})
    for p in (status or {}).get("probes") or []:
        b["probes"].append(p)
    if (status or {}).get("remote"):
        b["remote"] = status["remote"]
    return b


# ---------------------------------------------------------------- the manager

class RunManager:
    def __init__(self, runs_store, shell_factory, env=None, sleep=time.sleep, poll_seconds=10.0, on_change=None, root=".kcp-console",
                 tokens=None):
        self.store, self.shell_factory, self.env, self.root = runs_store, shell_factory, env, root
        self.tokens = tokens if tokens is not None else OperatorToken(env)  # one in-memory cache for every run
        self.sleep, self.poll_seconds = sleep, poll_seconds
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.runs = {r["id"]: r for r in runs_store.all()}
        for r in self.runs.values():             # a console restart orphans in-flight runs: say so
            if r.get("status") in ("queued", "running"):
                r["status"] = "interrupted"
        self.threads = []
        self.on_change = on_change

    def list(self) -> list:
        with self.lock:
            return sorted((dict(r) for r in self.runs.values()), key=lambda r: r.get("created", ""), reverse=True)

    def get(self, run_id) -> dict:
        with self.lock:
            r = self.runs.get(run_id)
            if r is None:
                raise RunError(404, "no such run")
            return dict(r)

    def _event(self, run, kind, text, **extra):
        ev = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "text": redact.text(text), **extra}
        with self.cond:
            run.setdefault("events", []).append(ev)
            self.store.event(run["id"], ev)
            self.store.save(run)
            self.cond.notify_all()
        if self.on_change:
            self.on_change(run)

    def prepare(self, cfg: dict, kind: str, name: str, params: dict, status: dict | None = None) -> dict:
        r = build_request(cfg, kind, name, params, status, root=self.root)
        run = {"id": new_id(), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "prepared",
               "cfg": model.merged(cfg), **r}
        if r["guards"]:
            run["status"] = "refused"
        with self.lock:
            self.runs[run["id"]] = run
        self._event(run, "prepared", f"{kind} {name}: request {r['request_sha256'][:12]}…"
                    + (f" REFUSED: {' '.join(r['guards'])}" if r["guards"] else ""))
        return self.public(run)

    def public(self, run) -> dict:
        return {k: v for k, v in run.items() if k != "cfg"}

    def approve(self, run_id: str, request_sha256: str, typed_cluster: str = "", approved: bool = False) -> dict:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise RunError(404, "no such run")
            if run["status"] != "prepared":
                raise RunError(409, f"run is {run['status']}, not prepared")
            if request_sha256 != run["request_sha256"]:
                raise RunError(409, "the request changed since it was prepared: prepare it again")
            if run["changes_state"] and not approved:
                raise RunError(403, "this request changes state: it needs an explicit approval")
            if run["needs_typed_cluster"] and typed_cluster != run["cluster"]:
                raise RunError(403, "destructive request: type the cluster name exactly to approve it")
            # Two changes on one cluster collide (the same test pod, the same helm release): one at a time.
            if run["changes_state"] and any(o is not run and o.get("config") == run.get("config") and o.get("changes_state")
                                            and o.get("status") in ("queued", "running") for o in self.runs.values()):
                raise RunError(409, f"a change on {run.get('config')} is already running: approve this one when it has finished")
            run["status"] = "queued"
            run["approved"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "changes_state": run["changes_state"]}
        self._event(run, "approved", "approved" if run["changes_state"] else "read-only: started")
        t = threading.Thread(target=self._work, args=(run,), daemon=True, name=f"run-{run_id}")
        self.threads.append(t)
        t.start()
        return self.public(run)

    def cancel(self, run_id) -> dict:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise RunError(404, "no such run")
            if run["status"] in ("prepared", "queued", "running"):
                run["cancel"] = True
                if run["status"] == "prepared":
                    run["status"] = "cancelled"
        self._event(run, "cancel", "cancel requested (an ADO run already queued keeps running in ADO)")
        return self.public(run)

    def _work(self, run):
        sh = self.shell_factory(lambda line: self._event(run, "log", line))
        try:
            if run["kind"] == "jfrog":
                self._jfrog(run, sh)
            elif run["kind"] == "direct":
                self._direct(run, sh)
            else:
                self._pipeline(run, sh)
        except (AdoError, RunError, ValueError, OSError) as exc:
            run["status"] = "error"
            run["error"] = redact.text(str(exc))
            self._event(run, "error", str(exc))
        finally:
            run["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.store.save(run)

    def _pipeline(self, run, sh):
        cfg = run["cfg"]
        ado = Ado(sh, cfg["ado"]["organization"], cfg["ado"]["project"])
        req = run["request"]
        if run["kind"] == "template":
            sv = ado.secret_variable(req["pipeline"])
            run["secret_variable"] = sv
            self._event(run, "check", f"JfrogDownloadToken: {sv['detail']}", status=sv["status"])
        q = ado.queue(req["pipeline"], req["branch"], req["parameters"])
        run.update({"ado_run_id": q["id"], "ado_url": q["url"], "status": "running"})
        self._event(run, "queued", f"ADO run {q['id']} queued")
        waited = 0.0
        while True:
            s = ado.show(q["id"])
            if s["status"] == "completed":
                break
            if run.get("cancel") or waited > POLL_LIMIT_SECONDS:
                run["status"] = "cancelled" if run.get("cancel") else "error"
                self._event(run, "stopped", "stopped polling")
                return
            self._event(run, "poll", f"ADO run {q['id']}: {s['status']}")
            self.sleep(self.poll_seconds)
            waited += self.poll_seconds
        text = ado.logs(q["id"])
        self.store.save_log(run["id"], text)
        run["result"] = {"ado_result": s["result"], "chars": len(text)}
        qs = summary.quick_summary(text) if ("QUICK SUMMARY" in text or "Node inspected" in text) else None
        if qs:
            run["result"]["summary_source"] = qs["source"]
            if qs["source"] == "full-log":
                self._event(run, "warn", "No QUICK SUMMARY footer: output was cut off (~50,000 characters). Parsed the full log.")
        fb = [f for f in events.parse_pod_events(text)["forbidden"] if f["user_principal"]]
        if fb:
            run["result"]["forbidden_user"] = fb[0]["user_principal"]
            self._event(run, "warn", f"Forbidden attributed to user {fb[0]['user_principal']}, not the service principal")
        run["status"] = "succeeded" if s["result"] == "succeeded" else "failed"
        self._event(run, "done", f"ADO result: {s['result']}")

    def _jfrog(self, run, sh):
        cfg = run["cfg"]
        jf = Jfrog(sh, cfg["jfrog"]["host"], tokens=self.tokens)
        p = run["request"]["parameters"]
        run["status"] = "running"
        if p["action"] == "create-provider":
            res = jf.create_provider(p["body"])
        elif p["action"] == "create-mapping":
            res = jf.create_mapping(p["target"], p["body"])
        elif p["action"] == "promote":
            res = jf.promote(p["target"], p["body"])
        elif p["action"] == "delete-mapping":
            res = jf.delete_mapping(p["target"], p["body"]["name"])
        else:
            res = jf.delete_provider(p["target"])
        run["result"] = {"status": res["status"], "body": redact.text(res["body"] or "")[:2000]}
        # 409 on a create: it is already there (Set up made it), which is the state the action wants.
        exists = res["status"] == 409 and p["action"].startswith("create")
        run["status"] = "succeeded" if res["ok"] or exists else "failed"
        self._event(run, "done", f"{p['action']}: HTTP {res['status']}")

    def _direct(self, run, sh):
        p = run["request"]["parameters"]
        run["status"] = "running"
        rn = direct.Runner(run["cfg"], sh, self.root, self.env, tokens=self.tokens,
                           log=lambda line: self._event(run, "log", line), dry_run=p["DryRun"], sleep=self.sleep)
        results = rn.execute(p["steps"], stop=lambda: bool(run.get("cancel")))
        run["result"] = {"steps": results}
        failed = [r for r in results if r["status"] == "failed"]
        if run.get("cancel"):
            run["status"] = "cancelled"
        elif failed:
            run["status"] = "failed"
        else:
            run["status"] = "succeeded" if len(results) == len(p["steps"]) else "failed"
        self._event(run, "done", f"{p['plan']}{' (dry run)' if p['DryRun'] else ''}: "
                    + ", ".join(f"{r['id']} {r['status']}" for r in results))

    def wait(self, run_id, timeout=10.0):
        """Tests: block until the run leaves queued/running."""
        end = time.monotonic() + timeout
        with self.cond:
            while self.runs[run_id]["status"] in ("queued", "running") and time.monotonic() < end:
                self.cond.wait(0.05)
        return self.get(run_id)
