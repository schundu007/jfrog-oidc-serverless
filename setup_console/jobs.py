"""Plan, approve, apply: one job at a time per stack, in a background thread.

A plan job runs `terraform plan` (or `plan -destroy`) and keeps the summary and the plan file's
sha256. The apply job runs only that plan file, through console.steps.terraform's gates: the
approval names the sha256, the file still hashes to it, and any delete needs the stack name typed.
Every log line goes through redaction; no token is ever logged or returned.

A workload stack with a stack-setup pipeline (pipelines/<kind>/<cloud>/<stack>/stack-setup.yml) never
runs Terraform here: Deploy and Destroy queue that pipeline in Azure DevOps, the plan shown is the
one the run made, and Approve answers the run's environment approval, so ADO applies exactly that
plan file. Foundations (they create the pipelines' trust) and reads of state still run here.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time

from console import redact
from console.shell import Shell
from console.steps.terraform import ApplyRefused, Terraform, TerraformError

from . import stacks
from .ado import AdoError


MAX_LOG = 5000   # lines kept per job
POLL_SECONDS = 10
RUN_TIMEOUT = 90 * 60   # a pipeline run that takes longer is reported, not waited on


class JobError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Jobs:
    def __init__(self, env_name: str = "test", root=stacks.STACKS_DIR, shell_factory=None, environ=None, token_source=None,
                 state_root=stacks.STATE_ROOT, ado=None, repo_root=stacks.REPO_ROOT, sleep=time.sleep):
        self.env_name, self.root, self.state_root = env_name, root, state_root
        self.shell_factory = shell_factory or (lambda log: Shell(mode="live", log=log))
        self.environ = os.environ if environ is None else environ
        self.token_source = token_source   # callable(shell, host) -> JFrog token, for stacks with a JFrog provider
        self._jobs, self._plans, self._status = {}, {}, {}
        self._lock = threading.Lock()
        self.ado, self.repo_root, self.sleep = ado, repo_root, sleep

    def pipeline_path(self, stack_id: str) -> str | None:
        """The stack's ADO stack-setup pipeline (repo-relative), when changes to it run in ADO."""
        if self.ado is None or stacks.depends_on(stack_id, self.root) is None:
            return None
        rel = f"pipelines/{stacks.rel_dir(stack_id)}/stack-setup.yml"
        return rel if (self.repo_root / rel).is_file() else None

    # -- reads -------------------------------------------------------------------

    def stacks(self) -> list:
        with self._lock:
            # The running job per stack, so a page that closed the drawer or reloaded can follow it again.
            running = {j["stack"]: {"id": j["id"], "action": j["action"]} for j in self._jobs.values() if j["state"] == "running"}
            # The latest finished job per stack, when it failed: the row warns and links to its log.
            last = {}
            for j in sorted(self._jobs.values(), key=lambda j: j["started"]):
                if j["state"] != "running" and j["action"] != "status":   # automatic state reads never warn
                    last[j["stack"]] = j
            failed = {sid: {"id": j["id"], "action": j["action"], "error": (j["error"] or "").splitlines()[0][:300] if j["error"] else ""}
                      for sid, j in last.items() if j["state"] == "failed" and j["action"] != "status"}
            out = []
            for s in stacks.CATALOGUE:
                sid = s["id"]
                needs, dependents = self._needs(sid), self._dependents(sid)
                out.append(dict(s, dir=f"terraform/stacks/{stacks.rel_dir(sid)}", status=self._status.get(sid), busy=sid in running, job=running.get(sid),
                                plan=self._public_plan(self._plans.get(sid)), failed=failed.get(sid),
                                depends_on=stacks.depends_on(sid, self.root), needs=needs, dependents=dependents,
                                missing=stacks.missing(sid, self.env_name, self.root, self.state_root)))
            return out

    # Callers hold self._lock.
    def _needs(self, sid) -> list:
        """The foundation this stack stands on, unless its state shows it deployed. Order is strict:
        only a foundation read as deployed satisfies it; not yet read counts as missing."""
        dep = stacks.depends_on(sid, self.root)
        return [dep] if dep and not (self._status.get(dep) or {}).get("deployed") else []

    def _dependents(self, sid) -> list:
        """Deployed stacks that stand on this one (destroy them first)."""
        return [x for x in stacks.IDS if stacks.depends_on(x, self.root) == sid and (self._status.get(x) or {}).get("deployed")]

    def get(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                raise JobError(404, "no such job")
            return {k: (list(v) if k in ("log", "log_times") else v) for k, v in j.items() if not k.startswith("_")}

    @staticmethod
    def _public_plan(p):
        if not p:
            return None
        return {k: p[k] for k in ("action", "sha256", "summary", "changes", "destructive", "confirmation_phrase", "job")}

    # -- actions -----------------------------------------------------------------

    def audit(self, stack_id: str, action, client) -> None:
        """Who asked for a change, kept outside git: every deploy, destroy, approval and discard from
        the page, so an unexpected change can be traced to the browser that asked for it."""
        if action == "status":
            return
        line = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stack": str(stack_id)[:64],
                "action": str(action)[:20], "client": redact.text(str(client or ""))[:200]}
        p = stacks.Path(self.state_root) / self.env_name / "audit.jsonl"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass

    def start(self, stack_id: str, action: str, approval=None) -> dict:
        stacks.get(stack_id)
        if action not in ("plan-apply", "plan-destroy", "apply", "status"):
            raise JobError(400, "action must be plan-apply, plan-destroy, apply or status")
        if action != "apply":
            stacks.tf_args(stack_id, self.env_name, self.root, self.state_root)   # inputs filled in, state derivable
        with self._lock:
            if any(j["stack"] == stack_id and j["state"] == "running" for j in self._jobs.values()):
                raise JobError(409, f"{stack_id} is busy")
            if action == "plan-apply" and self._needs(stack_id):
                dep = stacks.get(self._needs(stack_id)[0])
                raise JobError(409, f"deploy {dep['kind']} · {dep['group']} · {dep['title']} ({dep['id']}) first: {stack_id} keeps its state there")
            if action == "plan-destroy" and self._dependents(stack_id):
                names = ", ".join(stacks.get(x)["title"] + f" ({x})" for x in self._dependents(stack_id))
                raise JobError(409, f"destroy what stands on {stack_id} first: {names}")
            plan = self._plans.get(stack_id)
            if action == "apply":
                if not plan:
                    raise JobError(409, "plan first")
                try:   # refuse before any thread or subprocess
                    if "_ado" in plan:
                        _check_ado_approval(plan, approval)
                    else:
                        plan["_tf"].check_approval(plan, approval)
                except ApplyRefused as exc:
                    raise JobError(403, str(exc)) from None
            job = {"id": secrets.token_hex(6), "stack": stack_id, "action": action, "state": "running",
                   "started": time.time(), "log": [], "log_times": [], "result": None, "error": None}
            self._jobs[job["id"]] = job
        threading.Thread(target=self._work, args=(job, approval), name=f"setup-{stack_id}", daemon=True).start()
        return self.get(job["id"])

    def _say(self, job, text, *details):
        """One plain-words progress line in the job's log (Terraform's own output streams in between),
        then any detail lines under it, unmarked."""
        with self._lock:
            _append(job, ["▶ " + redact.text(text), *(redact.text(d) for d in details)])

    def _where(self, stack_id, init_args) -> str:
        """Where this stack's Terraform state lives, in words."""
        pairs = dict(a[len("-backend-config="):].split("=", 1) for a in init_args if a.startswith("-backend-config="))
        if "bucket" in pairs:
            return f"Cloud Storage bucket {pairs['bucket']}, prefix {pairs.get('prefix', '')}"
        if "storage_account_name" in pairs:
            return (f"storage account {pairs['storage_account_name']}, container {pairs.get('container_name', '')}, "
                    f"key {pairs.get('key', '')}")
        return f"local file terraform/stacks/{stacks.rel_dir(stack_id)}/terraform.tfstate"

    def _terraform(self, job) -> Terraform:
        # The plan's Terraform is reused by the apply job: its log goes to whichever job runs it now.
        current = {"job": job}

        def log(line):
            with self._lock:
                _append(current["job"], [redact.text(line)])
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
        name = f"{meta['kind']} · {meta['group']} · {meta['title']} ({sid}), environment {self.env_name}"
        try:
            if action in ("plan-apply", "plan-destroy", "apply") and self.pipeline_path(sid):
                result = self._work_ado(job, name, approval)
                self._finish(job, "done", result=result)
                return
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
                    self._say(job, "Deployed.")  # Terraform printed the outputs just above
            elif action == "status":
                self._say(job, f"Checking {name}: reading its Terraform state.")
                tf = self._terraform(job)
                tf.init()
                outs = tf.outputs()
                result = {"deployed": bool(outs), "outputs": outs}
                self._set_status(sid, result)
                if outs:
                    self._say(job, "Deployed. Outputs:", *_outputs_lines(outs))
                else:
                    self._say(job, "Not deployed: the state holds nothing yet.")
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
            if action == "apply" or (action == "status" and result.get("deployed")):
                self._reread_dependents(sid)
        except (TerraformError, ApplyRefused, stacks.StackError, AdoError, OSError) as exc:
            msg = redact.text(str(exc))[:2000]
            first = msg.splitlines()[0] if msg else type(exc).__name__
            self._say(job, f"Failed: {first}")
            if action == "status":   # e.g. the state store does not exist yet: nothing is deployed there
                self._set_status(sid, {"deployed": False, "outputs": {}, "error": first[:300]})
            self._finish(job, "failed", error=msg)

    def _follow(self, job, run_id, until) -> dict:
        """Poll the run until its phase is in `until`; say each new phase once."""
        said, started = None, time.monotonic()
        words = {"planning": "Azure DevOps is planning…", "applying": "Azure DevOps is applying the approved plan…"}
        while True:
            st = self.ado.stack_run(run_id)
            if st["phase"] != said and st["phase"] in words:
                self._say(job, words[st["phase"]])
            said = st["phase"]
            if st["phase"] in until or st["phase"] in ("failed", "declined", "authorizing"):
                return st
            if time.monotonic() - started > RUN_TIMEOUT:
                raise AdoError(f"run {run_id} is still {st['phase']} after {RUN_TIMEOUT // 60} minutes: see {st['url']}")
            self.sleep(POLL_SECONDS)

    def _work_ado(self, job, name, approval) -> dict:
        sid, action = job["stack"], job["action"]
        if action == "apply":
            with self._lock:
                plan = self._plans.pop(sid)
            run, ap = plan["_ado"]["run"], plan["_ado"]["approval"]
            verb = "destroy" if plan["action"] == "destroy" else "deploy"
            self._say(job, f"Approving the {verb} plan of {name} in Azure DevOps (run {run}, plan {plan['sha256'][:12]}).")
            self.ado.answer(ap, True, f"Approved in the console: plan {plan['sha256'][:12]}")
            st = self._follow(job, run, ("done",))
            if st["phase"] != "done":
                raise AdoError(st.get("error") or f"run {run} was {st['phase']}: {st['url']}")
            destroyed = plan["action"] == "destroy"
            outs = {} if destroyed else st.get("outputs") or {}
            self._set_status(sid, {"deployed": not destroyed, "outputs": outs})
            self._say(job, "Destroyed." if destroyed else "Deployed.", *_outputs_lines(outs))
            return {"applied": True, "destroyed": destroyed, "outputs": outs, "run": st["url"]}
        destroy = action == "plan-destroy"
        stacks.tf_args(sid, self.env_name, self.root, self.state_root)   # inputs filled in, state derivable
        pid, pname = self.ado.pipeline(self.pipeline_path(sid))
        params = {"action": "destroy" if destroy else "apply", "environment": self.env_name,
                  "inputs": stacks.read_inputs(sid, self.env_name, self.state_root),
                  "state": stacks.foundation(stacks.backend_kind(sid, self.root), self.env_name, self.state_root)}
        run = self.ado.queue(pid, params)
        self._say(job, f"Planning a {'destroy' if destroy else 'deploy'} of {name} in Azure DevOps: "
                       f"{pname}, run {run} ({self.ado.run_url(run)}).")
        st = self._follow(job, run, ("waiting_approval", "done"))
        if st["phase"] == "done":
            self._say(job, "No changes: the stack already matches its inputs.")
            return {"action": "destroy" if destroy else "apply", "summary": {}, "changes": [], "run": st["url"]}
        if st["phase"] != "waiting_approval":
            raise AdoError(st.get("error") or f"run {run} was {st['phase']}: {st['url']}")
        p = {"action": "destroy" if destroy else "apply", "sha256": st["sha256"], "summary": st["summary"],
             "changes": st["changes"], "destructive": st["destructive"], "confirmation_phrase": sid,
             "job": job["id"], "_ado": {"run": run, "approval": st["approval"]}}
        with self._lock:
            self._plans[sid] = p
        self._say(job, f"Plan ready: {_summary_text(p['summary'])}. Nothing changes until you approve it.")
        return self._public_plan(p)

    def _reread_dependents(self, sid) -> None:
        """A foundation that was just deployed or destroyed changes what its stacks can read: read them again."""
        for x in stacks.IDS:
            if stacks.depends_on(x, self.root) == sid:
                try:
                    self.start(x, "status")
                except (JobError, stacks.StackError):
                    continue

    def refresh_all(self) -> list:
        """Read every stack's state in the background, so the list knows what is deployed without a
        Check button. Foundations first: a stack is read only once its foundation reads as deployed
        (that read starts it), so nothing is touched before what it stands on exists. Skips stacks with
        inputs still missing, a known status, or a running job."""
        started = []
        for sid in stacks.IDS:
            with self._lock:
                if sid in self._status or any(j["stack"] == sid and j["state"] == "running" for j in self._jobs.values()):
                    continue
                if self._needs(sid):
                    continue
            try:
                started.append(self.start(sid, "status")["id"])
            except (JobError, stacks.StackError):
                continue
        return started

    def discard(self, stack_id: str) -> None:
        stacks.get(stack_id)
        with self._lock:
            plan = self._plans.pop(stack_id, None)
        if plan and "_ado" in plan:   # the run would otherwise wait on its approval for days
            try:
                self.ado.answer(plan["_ado"]["approval"], False, "Discarded in the console")
            except AdoError:
                pass

    def _set_status(self, sid, status):
        with self._lock:
            self._status[sid] = dict(status, checked=time.time())

    def _finish(self, job, state, result=None, error=None):
        with self._lock:
            job.update(state=state, result=result, error=error, finished=time.time())


def _check_ado_approval(plan, approval) -> None:
    """The same gate as a local plan: the approval names the plan shown; deleting needs the name typed."""
    if not isinstance(approval, dict) or approval.get("plan_sha256") != plan["sha256"]:
        raise ApplyRefused("plan_hash_mismatch", "the approval is for a different plan; review the current plan")
    if plan.get("destructive") and approval.get("typed_confirmation") != plan["confirmation_phrase"]:
        raise ApplyRefused("typed_confirmation_required",
                           f"this plan deletes or replaces {len(plan['destructive'])} resource(s); "
                           f"type the stack name '{plan['confirmation_phrase']}' to confirm")


def _append(job, lines):
    """Add lines to a job's log, each stamped with when it arrived (log_times, epoch seconds, same length)."""
    now = time.time()
    job["log"].extend(lines)
    job.setdefault("log_times", []).extend(now for _ in lines)
    del job["log"][:-MAX_LOG]
    del job["log_times"][:-MAX_LOG]


def _summary_text(summary) -> str:
    words = {"create": "to add", "update": "to change", "replace": "to replace", "delete": "to destroy", "import": "to import"}
    parts = [f"{n} {words[k]}" for k, n in (summary or {}).items() if k in words and n]
    return ", ".join(parts) or "no changes"


def _outputs_lines(outputs) -> list:
    """Outputs as "name = value" lines (scalars; maps by entry), never sensitive ones (already "[sensitive]")."""
    rows = []
    for k, v in (outputs or {}).items():
        if isinstance(v, dict):
            rows += [f"{k}.{sk} = {sv}" for sk, sv in v.items() if not isinstance(sv, (dict, list))]
        elif v is not None and not isinstance(v, list):
            rows.append(f"{k} = {v}")
    return ["  " + r for r in rows]
