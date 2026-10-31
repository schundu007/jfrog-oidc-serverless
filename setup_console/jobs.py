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


MAX_LOG = 5000   # lines kept per job


class JobError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Jobs:
    def __init__(self, env_name: str = "test", root=stacks.STACKS_DIR, shell_factory=None, environ=None, token_source=None,
                 state_root=stacks.STATE_ROOT):
        self.env_name, self.root, self.state_root = env_name, root, state_root
        self.shell_factory = shell_factory or (lambda log: Shell(mode="live", log=log))
        self.environ = os.environ if environ is None else environ
        self.token_source = token_source   # callable(shell, host) -> JFrog token, for stacks with a JFrog provider
        self._jobs, self._plans, self._status = {}, {}, {}
        self._lock = threading.Lock()

    # -- reads -------------------------------------------------------------------

    def stacks(self) -> list:
        with self._lock:
            # The running job per stack, so a page that closed the drawer or reloaded can follow it again.
            running = {j["stack"]: {"id": j["id"], "action": j["action"]} for j in self._jobs.values() if j["state"] == "running"}
            return [dict(s, status=self._status.get(s["id"]), busy=s["id"] in running, job=running.get(s["id"]),
                         plan=self._public_plan(self._plans.get(s["id"])),
                         missing=stacks.missing(s["id"], self.env_name, self.root, self.state_root))
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
        if action != "apply":
            stacks.tf_args(stack_id, self.env_name, self.root, self.state_root)   # inputs filled in, state derivable
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

    def _say(self, job, text):
        """One plain-words progress line in the job's log (Terraform's own output streams in between)."""
        with self._lock:
            job["log"].append("▶ " + redact.text(text))
            del job["log"][:-MAX_LOG]

    def _where(self, stack_id, init_args) -> str:
        """Where this stack's Terraform state lives, in words."""
        pairs = dict(a[len("-backend-config="):].split("=", 1) for a in init_args if a.startswith("-backend-config="))
        if "bucket" in pairs:
            return f"Cloud Storage bucket {pairs['bucket']}, prefix {pairs.get('prefix', '')}"
        if "storage_account_name" in pairs:
            return (f"storage account {pairs['storage_account_name']}, container {pairs.get('container_name', '')}, "
                    f"key {pairs.get('key', '')}")
        return f"local file terraform/stacks/{stack_id}/terraform.tfstate"

    def _terraform(self, job) -> Terraform:
        # The plan's Terraform is reused by the apply job: its log goes to whichever job runs it now.
        current = {"job": job}

        def log(line):
            with self._lock:
                lines = current["job"]["log"]
                lines.append(redact.text(line))
                del lines[:-MAX_LOG]
        sh = self.shell_factory(log)
        env = dict(self.environ)
        init, plan = stacks.tf_args(job["stack"], self.env_name, self.root, self.state_root)
        self._say(job, f"Terraform state: {self._where(job['stack'], init)}.")
        host = stacks.jfrog_host(job["stack"], self.env_name, self.state_root)
        if not host:
            self._say(job, "JFrog: this stack has no JFrog resources.")
        elif env.get("JFROG_ACCESS_TOKEN"):
            self._say(job, f"JFrog {host}: using the admin token the portal was started with.")
        elif not stacks.needs_jfrog(job["stack"], job["action"], self.root):
            self._say(job, f"JFrog {host}: not needed, the state holds no JFrog resources.")
        elif self.token_source:
            self._say(job, f"JFrog {host}: getting a short-lived token from your OIDC login…")
            try:
                env["JFROG_ACCESS_TOKEN"] = self.token_source(sh, host)
                self._say(job, f"JFrog {host}: token received.")
            except Exception as exc:   # stacks without JFrog resources still plan
                self._say(job, f"JFrog {host}: no token ({redact.text(str(exc))[:200]}); continuing without it.")
        tf = Terraform(sh, stacks.stack_dir(job["stack"], self.root), env=env, init_args=init, plan_args=plan)
        tf.log_to = current
        return tf

    def _work(self, job, approval):
        sid, action = job["stack"], job["action"]
        meta = stacks.get(sid)
        name = f"{meta['group']} · {meta['title']} ({sid}), environment {self.env_name}"
        try:
            if action == "apply":
                with self._lock:
                    plan = self._plans.pop(sid)
                    plan["_tf"].log_to["job"] = job
                verb = "destroy" if plan["action"] == "destroy" else "deploy"
                self._say(job, f"Running the approved {verb} plan for {name} (plan {plan['sha256'][:12]}).")
                result = plan["_tf"].apply(plan, approval)
                result = {"applied": True, "destroyed": plan["action"] == "destroy", "outputs": result["outputs"]}
                self._set_status(sid, {"deployed": plan["action"] != "destroy", "outputs": {} if plan["action"] == "destroy" else result["outputs"]})
                if result["destroyed"]:
                    self._say(job, "Destroyed: nothing of this stack is left in its Terraform state.")
                else:
                    self._say(job, f"Deployed. {_outputs_text(result['outputs'])}")
            elif action == "status":
                self._say(job, f"Checking {name}: reading its Terraform state.")
                tf = self._terraform(job)
                tf.init()
                outs = tf.outputs()
                result = {"deployed": bool(outs), "outputs": outs}
                self._set_status(sid, result)
                self._say(job, f"Deployed. {_outputs_text(outs)}" if outs else "Not deployed: the state holds nothing yet.")
            else:
                destroy = action == "plan-destroy"
                self._say(job, f"Planning a {'destroy' if destroy else 'deploy'} of {name} with the saved inputs.")
                tf = self._terraform(job)
                self._say(job, "Terraform: initialising providers and state, then planning…")
                p = tf.plan(destroy=destroy)
                p.update(action="destroy" if destroy else "apply", job=job["id"], _tf=tf)
                with self._lock:
                    self._plans[sid] = p
                result = self._public_plan(p)
                self._say(job, f"Plan ready: {_summary_text(p.get('summary'))}. Nothing changes until you approve it.")
            self._finish(job, "done", result=result)
        except (TerraformError, ApplyRefused, stacks.StackError, OSError) as exc:
            msg = redact.text(str(exc))[:2000]
            first = msg.splitlines()[0] if msg else type(exc).__name__
            self._say(job, f"Failed: {first}")
            if action == "status":   # e.g. the state store does not exist yet: nothing is deployed there
                self._set_status(sid, {"deployed": False, "outputs": {}, "error": first[:300]})
            self._finish(job, "failed", error=msg)

    def refresh_all(self) -> list:
        """Read every stack's state in the background, so the list knows what is deployed without a
        Check button. Skips stacks with inputs still missing, a known status, or a running job."""
        started = []
        for sid in stacks.IDS:
            with self._lock:
                if sid in self._status or any(j["stack"] == sid and j["state"] == "running" for j in self._jobs.values()):
                    continue
            try:
                started.append(self.start(sid, "status")["id"])
            except (JobError, stacks.StackError):
                continue
        return started

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


def _summary_text(summary) -> str:
    words = {"create": "to add", "update": "to change", "replace": "to replace", "delete": "to destroy", "import": "to import"}
    parts = [f"{n} {words[k]}" for k, n in (summary or {}).items() if k in words and n]
    return ", ".join(parts) or "no changes"


def _outputs_text(outputs) -> str:
    """Outputs as "name = value" (scalars; maps by entry), never sensitive ones (already "[sensitive]")."""
    rows = []
    for k, v in (outputs or {}).items():
        if isinstance(v, dict):
            rows += [f"{k}.{sk} = {sv}" for sk, sv in v.items() if not isinstance(sv, (dict, list))]
        elif v is not None and not isinstance(v, list):
            rows.append(f"{k} = {v}")
    return ("Outputs: " + "; ".join(rows) + ".") if rows else "No outputs."
