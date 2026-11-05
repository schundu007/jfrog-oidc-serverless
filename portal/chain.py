"""Step 5 of a Deploy on Infrastructure: onboard the workload the stack just built and deployed.

The Infrastructure page (setup_console) knows stacks; the Serverless console knows workloads,
their configs and the Deliver run that publishes the onboarding workspace. The portal hosts both,
so it joins them here: after build-deploy, the workload is onboarded (already, with the same
identity) or a Deliver run starts for it, and the user approves that run on the workload page.
"""

from __future__ import annotations

import copy
import time

from console import model
from console.runner import DELIVERY_STEPS, RunError

# Workload stack → console kind (the Serverless + Create tiles map the other way).
KIND_OF = {
    "gcp-cloud-run-service": "gcp_run_service", "gcp-cloud-run": "gcp_run_job",
    "gcp-cloud-run-function": "gcp_run_function", "gcp-cloud-run-worker-pool": "gcp_run_worker_pool",
    "azure-container-app": "azure_container_app", "azure-container-apps": "azure_container_app_job",
    "azure-functions-aca": "azure_functions_aca", "azure-functions-premium": "azure_functions_premium",
    "azure-functions-flex": "azure_functions_flex", "azure-web-app": "azure_web_app",
}
NAME_OUTPUTS = ("app_name", "job_name", "function_name", "service_name", "worker_pool_name")


def target_of(kind: str, workload: str, inputs: dict, outputs: dict) -> dict:
    cloud = model.KINDS[kind]["cloud"]
    name = next((outputs[k] for k in NAME_OUTPUTS if outputs.get(k)), None) or workload
    if cloud == "azure":
        where = {"subscription": inputs.get("azure_subscription_id") or "",
                 "resource_group": outputs.get("resource_group") or f"rg-{workload}", "name": name}
    else:
        where = {"project": inputs.get("gcp_project_id") or "", "region": inputs.get("gcp_region") or "", "name": name}
    return {"kind": kind, cloud: where}


DELIVER_TIMEOUT = 20 * 60


def onboarder(serverless, clock=time.monotonic):
    """The Jobs.onboard hook for this portal's Serverless console."""

    def onboard(stack_id: str, workload: str, inputs: dict, outputs: dict) -> dict | None:
        kind = KIND_OF.get(stack_id.split("@")[0])
        if not kind:
            return None
        cloud = model.KINDS[kind]["cloud"]
        page = f"/serverless/{cloud}/w/{workload}"
        client = outputs.get("workload_identity_client_id")
        done = serverless.delivered().get(workload)
        if done and model.get_path(done, "target.kind") == kind:
            mapped = model.get_path(done, "target.discovered.client_id")
            if not client or not mapped or mapped == client:
                return {"state": "done", "text": "Onboarded: its identity already maps to JFrog.", "link": page}
        # A config for it: the saved one, else the delivered one, else this cloud's settings from another workload.
        try:
            cfg = serverless.store.get(workload)
        except FileNotFoundError:
            cfg = copy.deepcopy(done) if done else None
        if cfg is None:
            same = [c for c in serverless.store.list() if model.KINDS.get(c.get("kind"), {}).get("cloud") == cloud]
            if not same:
                return {"state": "needs_input", "text": "Onboard it on the Serverless page (no JFrog settings saved yet).",
                        "link": f"/serverless/{cloud}/new?kind={kind}"}
            base = serverless.store.get(same[0]["name"])
            cfg = {k: copy.deepcopy(v) for k, v in base.items() if k not in ("name", "target")}
        cfg["target"] = target_of(kind, workload, inputs, outputs)   # rediscovered by Deliver
        cfg = serverless.store.put(workload, cfg)
        try:
            run = serverless.runs.start(cfg, DELIVERY_STEPS)
        except RunError as exc:
            return {"state": "needs_input", "text": f"Onboard it on its workload page: {exc}", "link": page}
        runs = f"{page}?tab=runs"
        # Its Deliver run checks the target and publishes the onboarding workspace (a minute or two); the
        # Deploy follows it, then the onboarding pipeline that the publish starts.
        deadline = clock() + DELIVER_TIMEOUT
        with run.cond:
            while run.status == "running" and clock() < deadline:
                run.cond.wait(timeout=5)
        if run.status == "running":
            return {"state": "started", "text": "Onboarding still running: follow it on the workload page.", "link": runs}
        if run.status != "succeeded":
            bad = next((s for s in run.steps.values() if s.get("status") in ("failed", "needs_approval")), {})
            why = bad.get("error") or (f"waits for an approval of {bad['name']}" if bad else run.status)
            return {"state": "failed" if run.status == "failed" else "started",
                    "text": f"Onboarding {bad.get('title', '').lower() or 'run'}: {why}", "link": runs}
        res = (run.steps.get("publish") or {}).get("evidence", {}).get("result") or {}
        if (res.get("pipeline") or {}).get("run"):
            return {"state": "published", "ado_run": res["pipeline"]["run"], "link": runs}
        if res.get("commit"):
            return {"state": "published", "commit": res["commit"], "link": runs}
        if res.get("pull_request"):
            return {"state": "started", "text": "Pull request opened: merging it onboards the workload.",
                    "link": res["pull_request"]["url"]}
        return {"state": "failed", "text": "Onboarding published nothing.", "link": runs}

    return onboard
