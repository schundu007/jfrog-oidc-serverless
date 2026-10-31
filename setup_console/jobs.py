"""Plan, approve, apply: one job at a time per stack, in a background thread.

A plan job runs `terraform plan` (or `plan -destroy`) and keeps the summary and the plan file's
sha256. The apply job runs only that plan file, through console.steps.terraform's gates: the
approval names the sha256, the file still hashes to it, and any delete needs the stack name typed.
Every log line goes through redaction; no token is ever logged or returned.
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from console import redact
from console.shell import Shell
from console.steps.terraform import ApplyRefused, Terraform, TerraformError

from . import stacks


class JobError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Jobs:
    def __init__(self, env_name: str = "test", root=stacks.STACKS_DIR, shell_factory=None, environ=None, token_source=None):
        self.env_name, self.root = env_name, root
        self.shell_factory = shell_factory or (lambda log: Shell(mode="live", log=log))
        self.environ = os.environ if environ is None else environ
        self.token_source = token_source   # callable(shell, host) -> JFrog token, for stacks with a JFrog provider
        self._jobs, self._plans, self._status = {}, {}, {}
        self._lock = threading.Lock()

    # -- reads -------------------------------------------------------------------

    def stacks(self) -> list:
        with self._lock:
            busy = {j["stack"] for j in self._jobs.values() if j["state"] == "running"}
            return [dict(s, status=self._status.get(s["id"]), busy=s["id"] in busy,
                         plan=self._public_plan(self._plans.get(s["id"])))
                    for s in stacks.CATALOGUE]

    def get(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                raise JobError(404, "no such job")
            return {k: (list(v) if k == "log" else v) for k, v in j.items() if not k.startswith("_")}

    @staticmethod
    def _public_plan(p):
        if not p:
            return None
        return {k: p[k] for k in ("action", "sha256", "summary", "changes", "destructive", "confirmation_phrase", "job")}

    # -- actions -----------------------------------------------------------------

    def start(self, stack_id: str, action: str, approval=None) -> dict:
        stacks.get(stack_id)
        if action not in ("plan-apply", "plan-destroy", "apply", "status"):
            raise JobError(400, "action must be plan-apply, plan-destroy, apply or status")
        with self._lock:
            if any(j["stack"] == stack_id and j["state"] == "running" for j in self._jobs.values()):
                raise JobError(409, f"{stack_id} is busy")
            plan = self._plans.get(stack_id)
            if action == "apply":
                if not plan:
                    raise JobError(409, "plan first")
                tf = plan["_tf"]
                try:
                    tf.check_approval(plan, approval)   # refuse before any thread or subprocess
                except ApplyRefused as exc:
                    raise JobError(403, str(exc)) from None
            job = {"id": secrets.token_hex(6), "stack": stack_id, "action": action, "state": "running",
                   "started": time.time(), "log": [], "result": None, "error": None}
            self._jobs[job["id"]] = job
        threading.Thread(target=self._work, args=(job, approval), name=f"setup-{stack_id}", daemon=True).start()
        return self.get(job["id"])

    def _terraform(self, job) -> Terraform:
        def log(line):
            with self._lock:
                job["log"].append(redact.text(line))
        sh = self.shell_factory(log)
        env = dict(self.environ)
        host = stacks.jfrog_host(job["stack"], self.env_name, self.root)
        if not env.get("JFROG_ACCESS_TOKEN") and self.token_source and host:
            try:
                env["JFROG_ACCESS_TOKEN"] = self.token_source(sh, host)
            except Exception as exc:   # stacks without JFrog resources still plan
                log(f"no JFrog token: {redact.text(str(exc))[:200]}")
        init, plan = stacks.tf_args(job["stack"], self.env_name, self.root)
        return Terraform(sh, stacks.stack_dir(job["stack"], self.root), env=env, init_args=init, plan_args=plan)

    def _work(self, job, approval):
        sid, action = job["stack"], job["action"]
        try:
            if action == "apply":
                with self._lock:
                    plan = self._plans.pop(sid)
                result = plan["_tf"].apply(plan, approval)
                result = {"applied": True, "destroyed": plan["action"] == "destroy", "outputs": result["outputs"]}
                self._set_status(sid, {"deployed": plan["action"] != "destroy", "outputs": {} if plan["action"] == "destroy" else result["outputs"]})
            elif action == "status":
                tf = self._terraform(job)
                tf.init()
                outs = tf.outputs()
                result = {"deployed": bool(outs), "outputs": outs}
                self._set_status(sid, result)
            else:
                tf = self._terraform(job)
                destroy = action == "plan-destroy"
                p = tf.plan(destroy=destroy)
                p.update(action="destroy" if destroy else "apply", job=job["id"], _tf=tf)
                with self._lock:
                    self._plans[sid] = p
                result = self._public_plan(p)
            self._finish(job, "done", result=result)
        except (TerraformError, ApplyRefused, OSError) as exc:
            self._finish(job, "failed", error=redact.text(str(exc))[:2000])

    def discard(self, stack_id: str) -> None:
        stacks.get(stack_id)
        with self._lock:
            self._plans.pop(stack_id, None)

    def _set_status(self, sid, status):
        with self._lock:
            self._status[sid] = dict(status, checked=time.time())

    def _finish(self, job, state, result=None, error=None):
        with self._lock:
            job.update(state=state, result=result, error=error, finished=time.time())
