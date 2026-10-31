"""Deploy a workload the Serverless console looked for and did not find, after the operator says Yes.

The console's run asks (Discover: "Deploy the missing ...? Yes / No") and passes each answer here.
Everything runs in Azure DevOps through the workload type's own pipelines, like the Infrastructure
page's Deploy: the stack-setup pipeline plans, the operator's Yes approves exactly the plan shown,
ADO applies it. Types that run an image are deployed in two passes, as by hand: the stack without
the workload, the build-deploy pipeline for the first image (base image through JFrog), then the
stack again with that image. Function apps that take code get the build-deploy pipeline after the stack.
A foundation is never deployed from here: it holds the pipelines' trust (Infrastructure page only).
"""

from __future__ import annotations

import re
import time

from . import stacks
from .ado import AdoError

# Console kind -> (stack, the stack's first-artifact input, its deploy switch). None: no image pass.
STACKS = {
    "gcp_run_job": ("gcp-cloud-run", "job_image", "deploy_job"),
    "gcp_run_service": ("gcp-cloud-run-service", "service_image", "deploy_service"),
    "gcp_run_function": ("gcp-cloud-run-function", "source_object", "deploy_function"),
    "azure_container_app_job": ("azure-container-apps", "job_image", "deploy_job"),
    "azure_container_app": ("azure-container-app", "app_image", "deploy_app"),
    "azure_functions_aca": ("azure-functions-aca", "function_image", "deploy_function"),
    "azure_functions_premium": ("azure-functions-premium", None, None),
    "azure_functions_flex": ("azure-functions-flex", None, None),
}
# The stack names its resources from `workload`: caj-<workload>, <workload>-<6 hex> for function apps.
_PREFIX = {"azure_container_app_job": "caj-"}
_SUFFIXED = {"azure_functions_premium", "azure_functions_flex"}
_FIRST = re.compile(r"(?:pushed|uploaded) (\S+?)\.(?:\s|$)")
JOB_POLL, BUILD_POLL, BUILD_TIMEOUT = 2, 15, 60 * 60


class ProvisionError(RuntimeError):
    pass


class Declined(RuntimeError):
    def __init__(self):
        super().__init__("not deployed: the operator answered No")


def workload_name(kind: str, name: str) -> str:
    pre = _PREFIX.get(kind, "")
    if pre and name.startswith(pre):
        name = name[len(pre):]
    if kind in _SUFFIXED:
        name = re.sub(r"-[0-9a-f]{6}$", "", name)
    return name


def first_artifact(warnings: list) -> str | None:
    """The image (or source package) a build-deploy run made for a workload that did not exist yet."""
    for w in warnings:
        m = _FIRST.search(w)
        if m:
            return m.group(1)
    return None


class Provisioner:
    def __init__(self, jobs, ado, sleep=time.sleep):
        self.jobs, self.ado, self.sleep = jobs, ado, sleep

    def stack_for(self, cfg: dict) -> str | None:
        spec = STACKS.get(((cfg or {}).get("target") or {}).get("kind"))
        return spec[0] if spec else None

    def run(self, cfg: dict, ask, log) -> dict:
        """ask(info) -> {"answer": "yes"|"no", "plan_sha256", "typed_confirmation"}: blocks until answered."""
        kind = cfg["target"]["kind"]
        sid, first_input, switch = STACKS[kind]
        meta = stacks.get(sid)
        dep = stacks.depends_on(sid, self.jobs.root)
        if dep and not self.jobs.deployed(dep):
            raise ProvisionError(f"{meta['title']} stands on {dep}, which is not deployed: deploy it on the Infrastructure page first")
        if not self.jobs.pipeline_path(sid):
            raise ProvisionError(f"{sid} has no Azure DevOps stack-setup pipeline to deploy it with")
        cloud = kind.split("_", 1)[0]
        t = cfg["target"].get(cloud) or {}
        workload = workload_name(kind, t.get("name") or cfg["name"])
        declared = {v["name"] for v in stacks.variables(sid, self.jobs.root)}
        # What the Inputs form would show: saved, else the cloud's shared values (deployer, project), else defaults.
        values = {f["name"]: f["current"] for f in stacks.form(sid, self.jobs.env_name, self.jobs.root, self.jobs.state_root)["fields"]
                  if "current" in f}
        values.update({k: v for k, v in {"workload": workload, "gcp_project_id": t.get("project"), "gcp_region": t.get("region"),
                                         "azure_subscription_id": t.get("subscription")}.items() if v and k in declared})
        where = " / ".join(x for x in (t.get("project") or t.get("subscription"), t.get("region") or t.get("resource_group")) if x)
        if switch:
            values[switch] = False
        self._save(sid, values)
        log(f"{meta['title']} {workload} does not exist: deploying it with the {sid} stack in Azure DevOps.")
        self._deploy(sid, ask, log, f"{meta['title']} {workload} is not in {where or 'the target'}. Deploy it? "
                     + ("This creates its identity first, then builds the first image, then the workload."
                        if switch else "This creates it, then deploys its code."))
        artifact = self._build(sid, workload, log)
        if switch:
            if not artifact:
                raise ProvisionError("the build-deploy run did not report a first image or package")
            values[first_input], values[switch] = artifact, True
            self._save(sid, values)
            self._deploy(sid, ask, log, f"Create {meta['title']} {workload} from {artifact}?")
        return {"stack": sid, "workload": workload, "artifact": artifact}

    def _save(self, sid, values):
        stacks.write_inputs(sid, self.jobs.env_name, values, self.jobs.root, self.jobs.state_root)

    def _wait(self, job) -> dict:
        while True:
            j = self.jobs.get(job["id"])
            if j["state"] != "running":
                if j["state"] == "failed":
                    raise ProvisionError(f"{j['stack']}: {(j['error'] or 'failed').splitlines()[0]}")
                return j
            self.sleep(JOB_POLL)

    def _deploy(self, sid, ask, log, question) -> None:
        plan = self._wait(self.jobs.start(sid, "plan-apply"))["result"] or {}
        if not plan.get("summary"):
            log(f"{sid}: nothing to change.")
            return
        info = {"kind": "deploy_missing", "question": question, "stack": sid, "plan_sha256": plan["sha256"],
                "summary": plan["summary"], "changes": plan.get("changes") or [], "destructive": plan.get("destructive") or [],
                "confirmation_phrase": plan.get("confirmation_phrase")}
        try:
            answer = ask(info) or {}
        except BaseException:
            self.jobs.discard(sid)   # cancelled: the ADO run must not wait on its approval
            raise
        if answer.get("answer") != "yes":
            self.jobs.discard(sid)
            raise Declined()
        self._wait(self.jobs.start(sid, "apply", {"plan_sha256": plan["sha256"],
                                                  "typed_confirmation": answer.get("typed_confirmation")}))
        log(f"{sid}: applied in Azure DevOps.")

    def _build(self, sid, workload, log) -> str | None:
        try:
            pid, name = self.ado.pipeline(f"pipelines/{stacks.rel_dir(sid)}/build-deploy.yml")
            run = self.ado.queue(pid, {"environment": self.jobs.env_name, "workload": workload})
        except AdoError as exc:
            raise ProvisionError(str(exc)) from None
        log(f"Building and rolling out {workload}: {name}, run {run} ({self.ado.run_url(run)}).")
        started = time.monotonic()
        while True:
            b = self.ado.build(run)
            if b["state"] == "completed":
                break
            if time.monotonic() - started > BUILD_TIMEOUT:
                raise ProvisionError(f"build run {run} is still running: {self.ado.run_url(run)}")
            self.sleep(BUILD_POLL)
        if b["result"] not in ("succeeded", "succeededWithIssues"):
            raise ProvisionError(f"build run {run} {b['result']}: {'; '.join(b['errors'])[:300] or self.ado.run_url(run)}")
        return first_artifact(b["warnings"])
