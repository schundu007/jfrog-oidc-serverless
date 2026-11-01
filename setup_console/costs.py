"""python -m setup_console.costs status|down|up [--cloud gcp|azure] [--only STACK ...] [--parallel N]

Takes down, and brings back, only the test infrastructure that costs money while it stands: every
serverless workload stack and the two test clusters. Their monitoring and logging go with them
(each Azure workload's Log Analytics workspace; the AKS cluster's Log Analytics workspace and
Container Insights rule; GKE's cluster logging and managed Prometheus). Foundations and the JFrog
trust stacks stay: they cost nothing, hold the pipelines' trust and the state stores, and keep the
saved inputs valid.

Every change goes through the Setup console's own gates (Jobs): a plan, then an apply of exactly
that plan, approved by its sha256 with the stack name typed. Stacks with a stack-setup pipeline run
in Azure DevOps, as from the page.

down  destroys each stack (already deleted is fine).
up    deploys each from its saved inputs (.console/setup/<env>/<stack>.tfvars.json). A workload
      whose image or source package lived in its own registry or bucket comes up the way a new one
      does: deploy with its switch off, run its build-deploy pipeline (it pushes the first image and
      names the input to set), set that input and the switch, deploy, run build-deploy again.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from console import redact

from . import stacks

COSTING = ("gcp-cloud-run", "gcp-cloud-run-service", "gcp-cloud-run-function", "gcp-cloud-run-worker-pool",
           "azure-container-apps", "azure-container-app", "azure-functions-aca", "azure-functions-premium",
           "azure-functions-flex", "gke-cluster", "aks-cluster")
CLOUD = {"gcp": "Google Cloud", "azure": "Azure"}
# The input that creates the workload once its first image or package exists.
SWITCHES = {"gcp-cloud-run": "deploy_job", "gcp-cloud-run-service": "deploy_service",
            "gcp-cloud-run-function": "deploy_function", "gcp-cloud-run-worker-pool": "deploy_worker_pool",
            "azure-container-apps": "deploy_job", "azure-container-app": "deploy_app",
            "azure-functions-aca": "deploy_function"}
# build-deploy's first-image warning: "... pushed X. In Setup, set <input> to it and <switch> on, ..."
FIRST = re.compile(r"(?:pushed|uploaded) (\S+?)\. In Setup, set (\w+) to it and (\w+) on")
POLL = 5
BUILD_TIMEOUT = 45 * 60


class CostError(RuntimeError):
    pass


class Runner:
    def __init__(self, jobs, ado, env: str, root=stacks.STACKS_DIR, state_root=stacks.STATE_ROOT,
                 sleep=time.sleep, out=print):
        self.jobs, self.ado, self.env, self.root, self.state_root = jobs, ado, env, root, state_root
        self.sleep, self._out, self._lock = sleep, out, threading.Lock()

    def say(self, sid, text):
        with self._lock:
            self._out(redact.text(f"{time.strftime('%H:%M:%S')}  {sid:<26} {text}"))

    # -- Setup jobs ---------------------------------------------------------------------------

    def job(self, sid, action, approval=None) -> dict:
        j = self.jobs.start(sid, action, approval)
        while j["state"] == "running":
            self.sleep(POLL)
            j = self.jobs.get(j["id"])
        if j["state"] != "done":
            raise CostError(f"{action} failed: {(j.get('error') or '').splitlines()[0][:400] if j.get('error') else 'no detail'}")
        return j.get("result") or {}

    def _change(self, sid, plan_action) -> str:
        verb = "destroy" if plan_action == "plan-destroy" else "deploy"
        self.say(sid, f"planning the {verb}…")
        plan = self.job(sid, plan_action)
        if plan.get("already_deleted"):
            return "already deleted"
        if not plan.get("sha256"):
            return "no changes"
        s = ", ".join(f"{n} {a}" for a, n in sorted((plan.get("summary") or {}).items()))
        self.say(sid, f"plan {plan['sha256'][:12]} ({s or 'outputs only'}): approving and applying…")
        self.job(sid, "apply", {"plan_sha256": plan["sha256"], "typed_confirmation": sid})
        return "destroyed" if verb == "destroy" else "deployed"

    def destroy(self, sid) -> str:
        return self._change(sid, "plan-destroy")

    def deploy(self, sid) -> str:
        return self._change(sid, "plan-apply")

    def read(self, sid) -> dict:
        return self.job(sid, "status")

    # -- build-deploy pipelines ---------------------------------------------------------------

    def build_pipeline(self, sid) -> str | None:
        rel = f"pipelines/{stacks.rel_dir(sid)}/build-deploy.yml"
        return rel if (stacks.REPO_ROOT / rel).is_file() else None

    def build_deploy(self, sid, workload) -> list:
        pid, _ = self.ado.pipeline(self.build_pipeline(sid))
        run = self.ado.queue(pid, {"environment": self.env, "workload": workload})
        self.say(sid, f"build-deploy run {run} queued…")
        started = time.monotonic()
        while True:
            b = self.ado.build(run)
            if b["state"] == "completed":
                break
            if time.monotonic() - started > BUILD_TIMEOUT:
                raise CostError(f"build-deploy run {run} still running after {BUILD_TIMEOUT // 60} minutes: {self.ado.run_url(run)}")
            self.sleep(POLL * 3)
        if b["result"] not in ("succeeded", "succeededWithIssues"):
            raise CostError(f"build-deploy run {run} {b['result']}: {'; '.join(b['errors'])[:400]} {self.ado.run_url(run)}")
        return b["warnings"]

    # -- one stack ----------------------------------------------------------------------------

    def up(self, sid) -> str:
        gaps = stacks.missing(sid, self.env, self.root, self.state_root)
        if gaps:   # a stack that gained an input since it was saved: fill it in Setup first
            raise CostError(f"inputs not set: {', '.join(gaps)} (fill them in Setup)")
        inputs = stacks.read_inputs(sid, self.env, self.state_root)
        switch = SWITCHES.get(sid)
        build = self.build_pipeline(sid) and inputs.get("workload")
        if not (switch and inputs.get(switch) and build):
            done = self.deploy(sid)
            if build:
                self.build_deploy(sid, inputs["workload"])
            return done
        try:
            stacks.write_inputs(sid, self.env, dict(inputs, **{switch: False}), self.root, self.state_root)
            self.say(sid, f"first pass with {switch} off (its registry or bucket is new)")
            self.deploy(sid)
            first = next((m for m in map(FIRST.search, self.build_deploy(sid, inputs["workload"])) if m), None)
            if first:
                inputs = dict(inputs, **{first[2]: first[1], first[3]: True})
                self.say(sid, f"{first[2]} = {first[1]}, {first[3]} on")
        finally:
            stacks.write_inputs(sid, self.env, inputs, self.root, self.state_root)
        self.deploy(sid)
        self.build_deploy(sid, inputs["workload"])
        return "deployed and rolled out"


def select(cloud=None, only=None) -> list:
    picked = [s for s in COSTING if not only or s in only]
    if cloud:
        picked = [s for s in picked if stacks.get(s)["group"] == CLOUD[cloud]]
    return picked


def run_all(runner: Runner, action: str, picked: list, parallel: int = 1) -> dict:
    # A workload keeps its state in its foundation's store: read the foundations first. One that is
    # gone (deleted outside the console) comes back first on up, with its cloud's JFrog trust, which
    # names the foundation's service connections; on down its workloads went with its store.
    for f in sorted({stacks.depends_on(s, runner.root) for s in picked} - {None}):
        if runner.read(f).get("deployed"):
            continue
        if action != "up":
            runner.say(f, "not deployed: its workloads' state store is gone with it")
            continue
        runner.say(f, runner.deploy(f))
        for trust in (s["id"] for s in stacks.CATALOGUE if s.get("after") == f):
            runner.say(trust, runner.deploy(trust))
        runner.read(f)

    def one(sid):
        try:
            if action == "status":
                r = runner.read(sid)
                res = "deployed" if r.get("deployed") else "not deployed"
            else:
                res = runner.destroy(sid) if action == "down" else runner.up(sid)
            runner.say(sid, res)
            return sid, res
        except Exception as exc:   # one stack failing never stops the others
            runner.say(sid, f"FAILED: {exc}")
            return sid, f"failed: {exc}"
    # Clusters take longest: start them first.
    order = sorted(picked, key=lambda s: stacks.get(s)["kind"] != "Kubernetes")
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        return dict(pool.map(one, order))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m setup_console.costs", description=__doc__.split("\n\n")[0])
    p.add_argument("action", choices=("status", "down", "up"))
    p.add_argument("--cloud", choices=tuple(CLOUD))
    p.add_argument("--only", nargs="+", choices=COSTING, metavar="STACK")
    p.add_argument("--parallel", type=int, default=3, help="stacks at once (ADO runs queue on its agents)")
    args = p.parse_args(argv)

    from console import deliver
    from console.shell import Shell
    from .ado import Ado
    from .jobs import Jobs
    delivery = deliver.load_settings(stacks.REPO_ROOT / "pipelines" / "delivery.json")
    env = delivery.get("environment") or "test"
    a = delivery["ado"]
    ado = Ado(Shell(mode="live", log=lambda line: None), a["organization"], a["project"],
              branch=a.get("target_branch") or "main", repo=a.get("repository") or "")
    from kcp_console.jfrog import OperatorToken   # the JFrog trust stacks' token, as the portal gets it
    runner = Runner(Jobs(env_name=env, ado=ado, token_source=OperatorToken().get), ado, env)
    picked = select(args.cloud, args.only)
    try:
        results = run_all(runner, args.action, picked, args.parallel)
    except CostError as exc:
        print(f"costs: {exc}", file=sys.stderr)
        return 1
    print()
    for sid in picked:
        print(f"{sid:<28} {results.get(sid)}")
    return 1 if any(str(r).startswith("failed") for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
