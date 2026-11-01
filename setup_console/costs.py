"""python -m setup_console.costs status|down|up [--cloud gcp|azure] [--only STACK ...] [--parallel N]

Takes down, and brings back, only the test infrastructure that costs money while it stands: every
serverless workload stack and the two test clusters. Their monitoring and logging go with them
(each Azure workload's Log Analytics workspace; the AKS cluster's Log Analytics workspace and
Container Insights rule; GKE's cluster logging and managed Prometheus). Foundations stay: they cost
nothing, hold the pipelines' trust and the state stores, and keep the saved inputs valid.

Infrastructure only: nothing here touches JFrog or a JFrog OIDC stack. Those have their own command,
python -m setup_console.oidc (after up: oidc up; before down: oidc down for any stack that stands on a
costing one, e.g. aks-jfrog-trust on aks-cluster; down refuses while one still does).

Every change goes through the Setup console's own gates (Jobs, setup_console/runner.py): a plan, then
an apply of exactly that plan, approved by its sha256 with the stack name typed. Stacks with a stack-setup pipeline run
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
import time
from concurrent.futures import ThreadPoolExecutor

from . import stacks
from .runner import POLL, RunError
from .runner import Runner as Gates

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
BUILD_TIMEOUT = 45 * 60


class CostError(RunError):
    pass


class Runner(Gates):
    error = CostError

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
            raise CostError(f"{sid}: inputs not set: {', '.join(gaps)} (fill them in Setup)")
        return self._up(sid)

    def down(self, sid) -> str:
        # Infrastructure knows nothing about JFrog OIDC; it only never pulls a stack out from under
        # another one that still stands on it (aks-jfrog-trust on aks-cluster).
        on = [s["id"] for s in stacks.CATALOGUE if s.get("after") == sid and self.deployed(s["id"])]
        if on:
            raise CostError(f"{', '.join(on)} still stands on it: take that down first "
                            f"(python -m setup_console.oidc down --only {' '.join(on)})")
        return self.destroy(sid)

    def _up(self, sid) -> str:
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
    # gone (deleted outside the console) comes back first on up; on down its workloads went with its store.
    for f in sorted({stacks.depends_on(s, runner.root) for s in picked} - {None}):
        if runner.deployed(f):
            continue
        if action != "up":
            runner.say(f, "not deployed: its workloads' state store is gone with it")
            continue
        runner.say(f, runner.deploy(f))
        runner.read(f)

    def one(sid):
        try:
            if action == "status":
                r = runner.read(sid)
                res = "deployed" if r.get("deployed") else "not deployed"
            else:
                res = runner.down(sid) if action == "down" else runner.up(sid)
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
    from console.store import default_root
    from portal.connections import Connections   # the sign-ins made on Settings, as the portal uses them
    Connections(default_root()).apply()
    delivery = deliver.load_settings(stacks.REPO_ROOT / "pipelines" / "delivery.json",
                                     overrides=default_root() / "connections" / "settings.json")
    env = delivery.get("environment") or "test"
    a = delivery["ado"]
    ado = Ado(Shell(mode="live", log=lambda line: None), a["organization"], a["project"],
              branch=a.get("target_branch") or "main", repo=a.get("repository") or "")
    runner = Runner(Jobs(env_name=env, ado=ado), ado, env)   # no JFrog token: infrastructure never needs one
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
