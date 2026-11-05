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
import subprocess
import threading
import re
import time

from console import redact
from console.shell import Shell
from console.steps.terraform import NOT_FOUND, ApplyRefused, Terraform, TerraformError

from . import stacks
from .ado import AdoError


def _alive_elsewhere(pid) -> bool:
    """Another live process on this machine (the portal's other server process) owns the record."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


MAX_LOG = 5000   # lines kept per job
JOB_ID = re.compile(r"[0-9a-f]{12}")
RETENTION_DAYS = 7   # runs of a stack that is gone stay in its archive this long, then are deleted
POLL_SECONDS = 10
ONBOARD_START_TIMEOUT = 5 * 60   # a push's CI trigger starts the onboarding run within this
RUN_TIMEOUT = 90 * 60   # a pipeline run that takes longer is reported, not waited on
# build-deploy's first-image warning: "... pushed X. In Setup, set <input> to it and <switch> on"
FIRST_IMAGE = re.compile(r"(?:pushed|uploaded) (\S+?)\. In Setup, set (\w+) to it and (\w+) on")


# The run's stages, named as Terraform and the release name them.
INIT, PLAN, OUTPUT = "terraform init", "terraform plan", "terraform output"
APPLY, DESTROY, APPROVAL = "terraform apply", "terraform destroy", "Approval"
BUILD, ONBOARD = "Build and deploy", "Onboarding and post-deployment check"
INITIALISED = "Terraform has been successfully initialized"


class JobError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Jobs:
    def __init__(self, env_name: str = "test", root=stacks.STACKS_DIR, shell_factory=None, environ=None, token_source=None,
                 state_root=stacks.STATE_ROOT, ado=None, repo_root=stacks.REPO_ROOT, sleep=time.sleep, probe=None):
        self.env_name, self.root, self.state_root = env_name, root, state_root
        stacks.use_env(env_name, state_root)   # descendants() sees this environment's extra deployments
        self.probe = probe or probe_store   # (kind, store) -> "missing" | "present" | "unknown"
        self.shell_factory = shell_factory or (lambda log: Shell(mode="live", log=log))
        self.environ = os.environ if environ is None else environ
        self.token_source = token_source   # callable(shell, host, cloud) -> JFrog token, for stacks with a JFrog provider
        self._jobs, self._plans, self._status = {}, {}, {}
        self._lock = threading.Lock()
        self.ado, self.repo_root, self.sleep = ado, repo_root, sleep

    def deployed(self, stack_id: str) -> bool:
        with self._lock:
            return bool((self._status.get(stack_id) or {}).get("deployed"))

    # Set by the portal: (stack, workload, inputs, outputs) -> {"state", "text", "link"} for a workload
    # with no onboarding workspace yet (starts its Deliver run).
    onboard = None

    def build_path(self, stack_id: str) -> str | None:
        """The workload stack's build-deploy pipeline (repo-relative), when it has one and runs in ADO."""
        if self.ado is None or stacks.depends_on(stack_id, self.root) is None:
            return None
        rel = f"pipelines/{stacks.rel_dir(stack_id)}/build-deploy.yml"
        return rel if (self.repo_root / rel).is_file() else None

    def onboard_target(self, stack_id: str, workload: str):
        """(onboarding pipeline, workspace) for a workload: the cloud's workload-onboard pipeline and the
        terraform/workspaces/<cloud>_* workspace whose configs name it; None when it is not onboarded yet."""
        cloud = stacks.rel_dir(stack_id).split("/")[1]
        pipe = f"pipelines/{cloud}-workload-onboard.yml"
        if not (self.repo_root / pipe).is_file():
            return None
        found = sorted((self.repo_root / "terraform" / "workspaces").glob(f"{cloud}_*/configs/{workload}.json"))
        return (pipe, found[0].parent.parent.relative_to(self.repo_root).as_posix()) if found else None

    def _run_pipeline(self, job, path, params, what) -> tuple:
        """Queue one ADO run, stream its log here and wait for it; raises when it does not succeed."""
        pid, pname = self.ado.pipeline(path)
        run = self.ado.queue(pid, params)
        self._say(job, f"{what}: {pname}, run {run} ({self.ado.run_url(run)}).")
        return self._follow_build(job, run, what)

    def _follow_build(self, job, run, what) -> tuple:
        """Stream one ADO run's log here until it completes; raises when it does not succeed."""
        seen, started = {}, time.monotonic()
        while True:
            b = self.ado.build(run)
            try:
                lines = self.ado.stream(run, seen)
            except AdoError:
                lines = []
            if lines:
                with self._lock:
                    _append(job, [redact.text(l) for l in lines])
            if b["state"] == "completed":
                break
            if time.monotonic() - started > RUN_TIMEOUT:
                raise AdoError(f"{what} run {run} still running after {RUN_TIMEOUT // 60} minutes: {self.ado.run_url(run)}")
            self.sleep(POLL_SECONDS)
        if b["result"] not in ("succeeded", "succeededWithIssues"):
            raise AdoError(f"{what} failed (run {run}): {'; '.join(b['errors'])[:600] or b['result']} {self.ado.run_url(run)}")
        return self.ado.run_url(run), b

    def _deliver(self, job, sid, outs) -> dict:
        """The release after the infrastructure, all in ADO: build-deploy (into the stack's deployment
        slot and swapped into production when the inputs name one), then the workload onboarding
        pipeline on its workspace (JFrog identity mapping, JFROG_* settings on production and every
        slot, post-deployment check of /healthz/jfrog). One Deploy click goes all the way; the only
        stops are the approvals that belong to the user."""
        path = self.build_path(sid)
        inputs = stacks.read_inputs(sid, self.env_name, self.state_root)
        workload = inputs.get("workload")
        if not (path and workload):
            return {}
        out = {}
        trust = [x for x in stacks.build_upstreams(sid, self.root)
                 if x != sid and x not in stacks.ancestors(sid, self.root)]
        with self._lock:
            gaps = [x for x in trust if not (self._status.get(x) or {}).get("deployed")]
        if gaps:
            names = ", ".join(stacks.get(x)["title"] for x in gaps)
            self._stage(job, BUILD, "waiting")
            self._say(job, f"Build and deploy waits: {names} not deployed (JFrog OIDC page).")
            return {"build": {"state": "blocked", "text": f"Deploy {names} first, then Deploy this again."}}
        params = {"environment": self.env_name, "workload": workload}
        slot = str(inputs.get("slot") or "").strip()
        if slot:   # deploy to the slot, then swap it into production
            params.update(slot=slot, swap=True)
        self._stage(job, BUILD, "running")
        url, b = self._run_pipeline(job, path, params, "Build and deploy")
        self._stage(job, BUILD, "succeeded", url)
        out["build"] = {"state": "done", "run": url}
        self._say(job, f"Built and deployed{f' to {slot}, swapped into production' if slot else ''}.")
        first = next((m for m in map(FIRST_IMAGE.search, b.get("warnings") or []) if m), None)
        if first:   # its registry was new: the workload is switched on with the image that now exists
            stacks.write_inputs(sid, self.env_name, dict(inputs, **{first[2]: first[1], first[3]: True}), self.root, self.state_root)
            self._say(job, f"First image in: {first[2]} = {first[1]}, {first[3]} on. Deploy again to start it.")
            out["build"]["again"] = True
            self._stage(job, ONBOARD, "skipped")
            return out
        target = self.onboard_target(sid, workload)
        self._stage(job, ONBOARD, "running")
        if target is None:   # no workspace yet: the portal starts its Deliver run (approved on the workload page)
            r = {"state": "needs_input", "text": f"{workload} is not onboarded yet: onboard it on its workload page."}
            if self.onboard:
                try:
                    r = self.onboard(sid, workload, inputs, outs) or r
                except Exception as exc:   # noqa: BLE001 - shown, never fatal to a deploy that worked
                    r = {"state": "failed", "text": redact.text(str(exc))[:300]}
            if r.get("state") == "published":   # pushed to the branch: its CI trigger runs the onboarding pipeline
                url = self._follow_onboard(job, sid, r)
                r = {"state": "done", "run": url, "text": "Onboarded; /healthz/jfrog ok on production and every slot."}
            self._say(job, r.get("text") or r["state"])
            self._stage(job, ONBOARD, {"done": "succeeded", "failed": "failed"}.get(r["state"], "waiting"), r.get("run") or r.get("link"))
            out["onboard"] = r
            return out
        pipe, ws = target
        url, _ = self._run_pipeline(job, pipe, {"workspace": ws}, "Onboarding and post-deployment check")
        self._stage(job, ONBOARD, "succeeded", url)
        out["onboard"] = {"state": "done", "run": url}
        self._say(job, "Onboarded; /healthz/jfrog ok on production and every slot.")
        return out

    def _follow_onboard(self, job, sid, r) -> str:
        """The onboarding run for what the Deliver run published: queued by it (no change), or started by
        the push's CI trigger (found by its commit). Streamed here like build-deploy."""
        cloud = stacks.rel_dir(sid).split("/")[1]
        pipe = f"pipelines/{cloud}-workload-onboard.yml"
        what = "Onboarding and post-deployment check"
        run, started = r.get("ado_run"), time.monotonic()
        while run is None:
            run = self.ado.run_for(pipe, r["commit"])
            if run is None:
                if time.monotonic() - started > ONBOARD_START_TIMEOUT:
                    raise AdoError(f"no {pipe} run started for commit {r['commit'][:12]} within "
                                   f"{ONBOARD_START_TIMEOUT // 60} minutes: check its trigger")
                self.sleep(POLL_SECONDS)
        self._say(job, f"{what}: run {run} ({self.ado.run_url(run)}).")
        url, _ = self._follow_build(job, int(run), what)
        return url

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
            running = {j["stack"]: {"id": j["id"], "action": j["action"], "op": j.get("op"),
                                    "stage": next((x["name"] for x in j.get("stages") or [] if x["state"] == "running"), None)}
                       for j in self._jobs.values() if j["state"] == "running"}
            # A state read (Refresh, automatic checks) changes nothing: the row says Checking, never Running.
            checking = {sid for sid, j in running.items() if j["action"] == "status"}
            running = {sid: j for sid, j in running.items() if sid not in checking}
            # The latest finished job per stack, when it failed: the row warns and links to its log.
            last = {}
            for j in sorted(self._jobs.values(), key=lambda j: j["started"]):
                if j["state"] != "running" and j["action"] != "status":   # automatic state reads never warn
                    last[j["stack"]] = j
            failed = {sid: {"id": j["id"], "action": j["action"], "error": (j["error"] or "").splitlines()[0][:300] if j["error"] else "",
                            "next": j.get("next")}
                      for sid, j in last.items() if j["state"] == "failed" and j["action"] != "status"}
            # The latest finished state read per stack: Last checked opens it.
            checks = {j["stack"]: j["id"] for j in sorted(self._jobs.values(), key=lambda j: j["started"])
                      if j["action"] == "status" and j["state"] != "running"}
            out = []
            for sid in stacks.all_ids(self.env_name, self.state_root):
                s = stacks.get(sid)
                needs, dependents, unchecked = self._needs(sid), self._dependents(sid), self._unchecked(sid)
                out.append(dict(s, dir=f"terraform/stacks/{stacks.rel_dir(sid)}", status=self._status.get(sid), busy=sid in running, job=running.get(sid), checking=sid in checking, check_job=checks.get(sid),
                                plan=self._public_plan(self._plans.get(sid)), failed=failed.get(sid),
                                depends_on=stacks.depends_on(sid, self.root), upstreams=list(stacks.ancestors(sid, self.root)),
                                needs=needs, dependents=dependents, unchecked=unchecked,
                                missing=stacks.missing(sid, self.env_name, self.root, self.state_root),
                                links=self._links(sid)))
            return out

    def _links(self, sid) -> dict:
        """Web pages for a row's cells: the stack's source in the ADO repo and, when it runs in ADO,
        its stack-setup pipeline. Built from known names only; no network call."""
        if self.ado is None:
            return {}
        out = {"source": getattr(self.ado, "source_url", lambda r: None)(f"terraform/stacks/{stacks.rel_dir(sid)}")}
        pipe = self.pipeline_path(sid)
        if pipe and hasattr(self.ado, "pipeline_url"):
            out["pipeline"] = self.ado.pipeline_url(pipe)
        return {k: v for k, v in out.items() if v}

    # Callers hold self._lock.
    def _needs(self, sid) -> list:
        """Every stack this one stands on, directly or not, that its state does not show deployed, in
        deploy order. Order is strict: only a stack read as deployed satisfies it; not yet read, or a
        read that failed, counts as missing."""
        return [a for a in stacks.ancestors(sid, self.root) if not (self._status.get(a) or {}).get("deployed")]

    def _dependents(self, sid) -> list:
        """Deployed stacks that stand on this one, directly or not (destroy them first), in destroy order."""
        return [x for x in stacks.descendants(sid, self.root) if (self._status.get(x) or {}).get("deployed")]

    def _unchecked(self, sid) -> list:
        """Stacks that stand on this one and may be deployed: inputs saved but state not read yet. A
        destroy waits until each is read; one never saved was never deployed from here."""
        return [x for x in stacks.descendants(sid, self.root) if x not in self._status
                and stacks.inputs_file(x, self.env_name, self.state_root).is_file()]

    def _blocked(self, sid, destroy: bool) -> str | None:
        """Why a deploy (or destroy) of this stack may not run now; None when every stack it stands on
        is deployed (and nothing stands on it). Callers hold self._lock."""
        if not destroy:
            needs = self._needs(sid)
            if needs:
                names = ", ".join(_name(x) for x in needs)
                return f"deploy {names} first: {sid} stands on {'it' if len(needs) == 1 else 'them'}"
            return None
        on, unread = self._dependents(sid), self._unchecked(sid)
        if on:
            return f"destroy what stands on {sid} first: {', '.join(_name(x) for x in on)}"
        if unread:
            return f"check what may stand on {sid} first: {', '.join(_name(x) for x in unread)}"
        return None

    def get(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                return {k: (list(v) if k in ("log", "log_times") else v) for k, v in j.items() if not k.startswith("_")}
        kept = self._read_kept(job_id)
        if not kept:
            old = self._read_archived(job_id)
            if old:
                return old
            raise JobError(404, "no such job")
        return self._settle(kept)

    def _settle(self, rec: dict) -> dict:
        """A record kept as running that no live job owns: the console restarted while it ran.
        A run in Azure DevOps went on without the console: its outcome is read there. A local one stopped."""
        if rec.get("state") != "running":
            return rec
        with self._lock:
            if rec.get("id") in self._jobs:
                return rec
        run = rec.get("ado_run")
        if run and self.ado:
            try:
                st = self.ado.stack_run(int(run))
            except (AdoError, ValueError, TypeError):
                return rec
            if st["phase"] == "waiting_approval" and rec.get("action") in ("plan-apply", "plan-destroy"):
                # The plan waits on its approval in ADO: take it back, so Approve still works.
                sid = rec["stack"]
                p = {"action": "destroy" if rec["action"] == "plan-destroy" else "apply", "sha256": st["sha256"],
                     "summary": st["summary"], "changes": st["changes"], "destructive": st["destructive"],
                     "confirmation_phrase": sid, "job": rec["id"],
                     "_ado": {"run": int(run), "approval": st["approval"], "seen": {}}}
                with self._lock:
                    self._plans.setdefault(sid, p)
                rec.update(state="done", finished=time.time(), result=self._public_plan(p))
                rec["log"] = list(rec.get("log") or []) + [f"▶ Plan ready: {_summary_text(p['summary'])}. Nothing changes until you approve it."]
                rec["log_times"] = list(rec.get("log_times") or []) + [time.time()]
                self._keep(rec)
                return rec
            if st["phase"] not in ("done", "declined", "failed"):
                return rec   # still going in Azure DevOps: read again later
            ok = st["phase"] == "done"
            line = ("▶ " + ("Finished in Azure DevOps while the console was restarting: " if ok else "Azure DevOps: ")
                    + (f"run {run} succeeded." if ok else (st.get("error") or f"run {run} was {st['phase']}.")))
            rec.update(state="done" if ok else "failed", finished=time.time(),
                       error=None if ok else redact.text(st.get("error") or f"run {run} was {st['phase']}"))
            if ok and rec.get("action") == "apply":
                rec["result"] = {"applied": True, "run": st.get("url")}
                self._set_status(rec["stack"], {"deployed": True, "outputs": st.get("outputs") or {}})
        else:
            if _alive_elsewhere(rec.get("pid")):
                return rec   # another console process runs it: read again later
            line = "▶ Stopped: the console restarted while this ran. Plan again."
            rec.update(state="failed", finished=time.time(), error="the console restarted while this ran")
        rec["log"] = list(rec.get("log") or []) + [line]
        rec["log_times"] = list(rec.get("log_times") or []) + [time.time()]
        self._keep(rec)
        return rec

    # -- history: every deploy, destroy and apply, kept after it finishes -----------------------------

    def _history_dir(self):
        return stacks.Path(self.state_root) / self.env_name / "jobs"

    def _read_kept(self, job_id: str):
        if not JOB_ID.fullmatch(str(job_id)):
            return None
        try:
            return json.loads((self._history_dir() / f"{job_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def _keep(self, job) -> None:
        """A change (not an automatic state read) on disk, outside git, so Runs lists it after a restart:
        written when it starts, when its ADO run is known and when it finishes. The log is redacted again;
        Terraform outputs are not kept."""
        if job["action"] == "status":
            return
        rec = {k: job.get(k) for k in ("id", "stack", "action", "op", "stages", "state", "started", "finished", "error", "next", "ado_run")}
        rec["pid"] = os.getpid()   # the process running it: another one sharing these records leaves it alone
        rec["error"] = redact.text(rec["error"]) if rec["error"] else None
        rec["log"] = [redact.text(x) for x in job.get("log", [])]
        rec["log_times"] = list(job.get("log_times", []))
        res = job.get("result")
        rec["result"] = {k: v for k, v in res.items() if k != "outputs"} if isinstance(res, dict) else None
        d = self._history_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{job['id']}.tmp"
            tmp.write_text(json.dumps(rec, default=str))
            os.replace(tmp, d / f"{job['id']}.json")
        except OSError:
            pass

    def history(self) -> list:
        """Every change run here or through its stack-setup pipeline, newest first, without logs."""
        fields = ("id", "stack", "action", "op", "state", "started", "finished", "error")
        out = {}
        try:
            files = list(self._history_dir().glob("*.json"))
        except OSError:
            files = []
        for f in files:
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and JOB_ID.fullmatch(str(rec.get("id", ""))):
                rec = self._settle(rec)
                out[rec["id"]] = {k: rec.get(k) for k in fields}
        with self._lock:
            for j in self._jobs.values():
                if j["action"] != "status":
                    out[j["id"]] = {k: j.get(k) for k in fields}
        rows = []
        for r in out.values():
            meta = _meta(r["stack"])
            if meta:
                r.update(kind=meta["kind"], group=meta["group"], title=meta["title"], cloud={"Google Cloud": "gcp", "Azure": "azure"}.get(meta["group"]),
                         error=(r["error"] or "").splitlines()[0][:300] if r["error"] else "")
                rows.append(r)
        return sorted(rows, key=lambda r: r["started"] or 0, reverse=True)

    # -- archive: the runs of a stack that is gone, kept apart for RETENTION_DAYS -------------------

    def _archive_dir(self):
        return stacks.Path(self.state_root) / self.env_name / "jobs-archive"

    def _read_archived(self, job_id: str):
        if not JOB_ID.fullmatch(str(job_id)):
            return None
        try:
            rec = json.loads((self._archive_dir() / f"{job_id}.json").read_text())
        except (OSError, ValueError):
            return None
        return dict(rec, archived=True) if isinstance(rec, dict) else None

    def remove_instance(self, sid: str) -> None:
        """Forget an extra deployment's saved inputs: only when it stands nowhere (read as not deployed,
        or never deployed) and nothing runs or waits for it. Its state, if any, was destroyed first."""
        if not stacks.get(sid).get("instance"):
            raise JobError(409, "only an added deployment can be removed")
        with self._lock:
            if any(j["stack"] == sid and j["state"] == "running" for j in self._jobs.values()) or sid in self._plans:
                raise JobError(409, "a run of it is still going: wait for it or discard its plan")
            if (self._status.get(sid) or {}).get("deployed"):
                raise JobError(409, "it is deployed: Destroy it first")
            self._status.pop(sid, None)
        stacks.inputs_file(sid, self.env_name, self.state_root).unlink(missing_ok=True)

    def archive_stack(self, sid: str, reason: str) -> list:
        """The stack is gone (destroyed, deleted outside, state forgotten, read as not deployed): its finished
        runs leave Runs for the archive. A running one, and the plan still waiting on Approve, stay."""
        with self._lock:
            keep = {j["id"] for j in self._jobs.values() if j["stack"] == sid and j["state"] == "running"}
            p = self._plans.get(sid)
            if p and p.get("job"):
                keep.add(p["job"])
        moved = []
        try:
            files = list(self._history_dir().glob("*.json"))
        except OSError:
            files = []
        now = time.time()
        for f in files:
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("stack") != sid or rec.get("id") in keep or rec.get("state") == "running":
                continue
            d = self._archive_dir()
            try:
                d.mkdir(parents=True, exist_ok=True)
                tmp = d / f".{rec['id']}.tmp"
                tmp.write_text(json.dumps(dict(rec, archived_at=now, archived_reason=reason), default=str))
                os.replace(tmp, d / f"{rec['id']}.json")
                f.unlink(missing_ok=True)
            except OSError:
                continue
            moved.append(rec["id"])
        with self._lock:
            for jid in moved:
                j = self._jobs.get(jid)
                if j and j["state"] != "running":
                    del self._jobs[jid]
        return moved

    def purge_archive(self, now=None) -> list:
        cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
        gone = []
        try:
            files = list(self._archive_dir().glob("*.json"))
        except OSError:
            return gone
        for f in files:
            try:
                rec = json.loads(f.read_text())
                if (rec.get("archived_at") or 0) <= cutoff:
                    f.unlink(missing_ok=True)
                    gone.append(f.stem)
            except (OSError, ValueError, AttributeError):
                continue
        return gone

    def archived(self) -> list:
        """The archived runs, newest first, without logs, each with when it is deleted."""
        self.purge_archive()
        fields = ("id", "stack", "action", "op", "state", "started", "finished", "error", "archived_at", "archived_reason")
        rows = []
        try:
            files = list(self._archive_dir().glob("*.json"))
        except OSError:
            files = []
        for f in files:
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            meta = _meta(rec.get("stack")) if isinstance(rec, dict) else None
            if not meta or not JOB_ID.fullmatch(str(rec.get("id", ""))):
                continue
            r = {k: rec.get(k) for k in fields}
            r.update(kind=meta["kind"], group=meta["group"], title=meta["title"],
                     cloud={"Google Cloud": "gcp", "Azure": "azure"}.get(meta["group"]),
                     error=(r["error"] or "").splitlines()[0][:300] if r["error"] else "",
                     expires_at=(r["archived_at"] or 0) + RETENTION_DAYS * 86400, archived=True)
            rows.append(r)
        return sorted(rows, key=lambda r: r["started"] or 0, reverse=True)

    def remove_archived(self, job_id: str) -> bool:
        """Delete one archived run (the portal keeps only the newest runs)."""
        if not JOB_ID.fullmatch(str(job_id)):
            return False
        f = self._archive_dir() / f"{job_id}.json"
        existed = f.is_file()
        f.unlink(missing_ok=True)
        return existed

    def remove(self, job_id: str) -> dict:
        """Forget one finished run: its record and log. A running one answers 409."""
        with self._lock:
            j = self._jobs.get(job_id)
            if j and j["state"] == "running":
                raise JobError(409, "the run is still going")
            if j:
                del self._jobs[job_id]
        f = self._history_dir() / f"{job_id}.json"
        if not JOB_ID.fullmatch(str(job_id)) or (not j and not f.is_file()):
            raise JobError(404, "no such job")
        try:
            f.unlink(missing_ok=True)
        except OSError as exc:
            raise JobError(500, f"could not delete the run: {exc.strerror}") from None
        return {"deleted": job_id}

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
        if action not in ("plan-apply", "plan-destroy", "apply", "status", "forget"):
            raise JobError(400, "action must be plan-apply, plan-destroy, apply, status or forget")
        if action == "forget":
            if not self._local_state(stack_id):
                raise JobError(409, f"{stack_id} has no local Terraform state to forget")
            if (approval or {}).get("typed_confirmation", "").strip() != stack_id:
                raise JobError(403, f"type {stack_id} to forget its state")
        elif action != "apply":
            stacks.tf_args(stack_id, self.env_name, self.root, self.state_root)   # inputs filled in, state derivable
        with self._lock:
            if any(j["stack"] == stack_id and j["state"] == "running" for j in self._jobs.values()):
                raise JobError(409, f"{stack_id} is busy")
            plan = self._plans.get(stack_id)
            # Upstream first, always: checked when planning and again when the plan is approved, since a
            # stack it stands on may have been destroyed (or one standing on it deployed) in between.
            if action in ("plan-apply", "plan-destroy") or (action == "apply" and plan):
                destroy = action == "plan-destroy" or (action == "apply" and plan["action"] == "destroy")
                why = self._blocked(stack_id, destroy)
                if why:
                    raise JobError(409, why)
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
            op = {"plan-apply": "apply", "plan-destroy": "destroy", "apply": (plan or {}).get("action", "apply")}.get(action, action)
            job = {"id": secrets.token_hex(6), "stack": stack_id, "action": action, "op": op, "state": "running",
                   "started": time.time(), "log": [], "log_times": [], "result": None, "error": None,
                   "stages": self._planned_stages(stack_id, action, op, plan)}
            self._jobs[job["id"]] = job
        self._keep(job)
        threading.Thread(target=self._work, args=(job, approval), name=f"setup-{stack_id}", daemon=True).start()
        return self.get(job["id"])

    def _planned_stages(self, sid, action, op, plan) -> list:
        """The run's stages up front, all pending: a Deploy or Destroy is init, plan, approval, then
        apply or destroy (a workload's Deploy then builds, deploys and onboards). The apply job carries
        the stages its plan job finished."""
        def pend(*names):
            return [{"name": n, "state": "pending", "started": None, "finished": None, "url": None} for n in names]
        if action == "status":
            return pend(INIT, OUTPUT)
        if action == "forget":
            return []
        if action == "apply":
            prior = (self._jobs.get((plan or {}).get("job")) or {}).get("stages")
            if prior:
                return [dict(x) for x in prior]
        tail = [DESTROY] if op == "destroy" else [APPLY]
        if op != "destroy" and self.build_path(sid) and (stacks.read_inputs(sid, self.env_name, self.state_root) or {}).get("workload"):
            tail += [BUILD, ONBOARD]
        return pend(INIT, PLAN, APPROVAL, *tail)

    def _stage(self, job, name, state, url=None) -> None:
        """Move one stage of the run on: pending → running → succeeded | failed | waiting | skipped."""
        now = time.time()
        with self._lock:
            st = next((x for x in job.setdefault("stages", []) if x["name"] == name), None)
            if st is None:
                st = {"name": name, "state": "pending", "started": None, "finished": None, "url": None}
                job["stages"].append(st)
            if state == "running" and not st["started"]:
                st["started"] = now
            if state in ("succeeded", "failed", "skipped"):
                st["started"] = st["started"] or now
                st["finished"] = now
            st["state"] = state
            if url:
                st["url"] = url

    def _stage_failed(self, job) -> None:
        """The stage that was running when the run failed."""
        for st in job.get("stages") or []:
            if st["state"] in ("running", "waiting"):
                self._stage(job, st["name"], "failed")
                return

    def _skip_rest(self, job, keep=()) -> None:
        """No changes (or nothing left to destroy): the stages after the plan do not run."""
        for st in job.get("stages") or []:
            if st["state"] == "pending" and st["name"] not in keep:
                self._stage(job, st["name"], "skipped")

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
        if stacks.get(job["stack"])["kind"] != "JFrog OIDC":
            # Infrastructure never touches JFrog: no JFrog token or setting reaches its Terraform.
            env = {k: v for k, v in env.items() if not k.startswith("JFROG_")}
        elif not host:
            self._say(job, "JFrog: no jfrog_url in the inputs.")
        elif env.get("JFROG_ACCESS_TOKEN"):
            self._say(job, f"JFrog {host}: using the admin token from Settings.")
        elif not stacks.needs_jfrog(job["stack"], job["action"], self.root):
            self._say(job, f"JFrog {host}: not needed, the state holds no JFrog resources.")
        elif self.token_source:
            self._say(job, f"JFrog {host}: getting a short-lived token from your OIDC login…")
            try:
                cloud = stacks.CLOUD_DIR[stacks.get(job["stack"])["group"]]   # each cloud's own operator
                env["JFROG_ACCESS_TOKEN"] = self.token_source(sh, host, cloud)
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
            if action == "forget":
                self._finish(job, "done", result=self._forget(job, meta))
                return
            # A workload's whole state lives in its foundation's store: with the store confirmed gone, the
            # deletion is validated and a read or a destroy passes, with no Terraform run and no ADO run.
            if action in ("status", "plan-destroy") and stacks.depends_on(sid, self.root):
                gone = self._store_gone(sid)
                if gone:
                    note = f"Deleted outside the console: {gone}."
                    self._say(job, f"Validated: {gone}, so nothing of {meta['title']} is left.")
                    if action == "plan-destroy":
                        self._say(job, "Already deleted: nothing to destroy.")
                    self._set_status(sid, {"deployed": False, "outputs": {}, "deleted": note})
                    self._finish(job, "done", result={"deployed": False, "outputs": {}, "already_deleted": True, "message": note})
                    return
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
                self._stage(job, APPROVAL, "succeeded")
                self._stage(job, DESTROY if plan["action"] == "destroy" else APPLY, "running")
                result = plan["_tf"].apply(plan, approval)
                self._stage(job, DESTROY if plan["action"] == "destroy" else APPLY, "succeeded")
                result = {"applied": True, "destroyed": plan["action"] == "destroy", "outputs": result["outputs"]}
                self._set_status(sid, {"deployed": plan["action"] != "destroy", "outputs": {} if plan["action"] == "destroy" else result["outputs"]})
                if result["destroyed"]:
                    self._say(job, "Destroyed: nothing of this stack is left in its Terraform state.")
                else:
                    self._say(job, "Deployed.")  # Terraform printed the outputs just above
            elif action == "status":
                self._say(job, f"Checking {name}: reading its Terraform state.")
                tf = self._terraform(job)
                self._stage(job, INIT, "running")
                tf.init()
                self._stage(job, INIT, "succeeded")
                self._stage(job, OUTPUT, "running")
                outs = tf.outputs()
                self._stage(job, OUTPUT, "succeeded")
                result = {"deployed": bool(outs), "outputs": outs}
                # A foundation's state is a local file: it can still list resources deleted outside the console.
                gone = self._store_gone(sid) if outs and not stacks.depends_on(sid, self.root) else None
                if gone:
                    result = {"deployed": False, "outputs": outs, "deleted": (
                        f"Partly deleted outside the console: {gone}. Its state still lists other resources; "
                        "Destroy checks each one and removes what is left, Deploy rebuilds it.")}
                    self._say(job, f"Validated: {gone}.", result["deleted"])
                self._set_status(sid, result)
                if gone:
                    self._say(job, "Not deployed as recorded: see above.")
                elif outs:
                    self._say(job, "Deployed. Outputs:", *_outputs_lines(outs))
                else:
                    self._say(job, "Not deployed: the state holds nothing yet.")
            else:
                destroy = action == "plan-destroy"
                self._say(job, f"Planning a {'destroy' if destroy else 'deploy'} of {name} with the saved inputs.")
                tf = self._terraform(job)
                self._say(job, "Terraform: initialising providers and state, then checking which resources still exist…")
                self._stage(job, INIT, "running")
                tf.init()
                self._stage(job, INIT, "succeeded")
                gone = tf.vanished()
                if gone:   # deleted outside the console: validated by the cloud, so the plan does not trip on them
                    tf.forget(gone)
                    self._say(job, f"Validated: {len(gone)} of its resources were already deleted outside the console; "
                                   "removed them from the state (Terraform kept a backup of it).", *gone)
                self._say(job, "Planning…")
                self._stage(job, PLAN, "running")
                p = tf.plan(destroy=destroy)
                self._stage(job, PLAN, "succeeded")
                if destroy and not p.get("changes"):
                    self._skip_rest(job)
                    note = "Deleted outside the console: nothing of it is left."
                    self._say(job, "Already deleted: nothing to destroy.")
                    self._set_status(sid, {"deployed": False, "outputs": {}, "deleted": note})
                    self._finish(job, "done", result={"deployed": False, "outputs": {}, "already_deleted": True, "message": note})
                    return
                if not destroy and not p.get("changes") and not p.get("outputs_change"):
                    # Nothing to approve: the stack already matches its inputs, so no plan waits on anyone.
                    self._say(job, "No changes: the stack already matches its inputs.")
                    self._skip_rest(job)
                    self._finish(job, "done", result={"action": "apply", "summary": p.get("summary") or {}, "changes": [],
                                                      "no_changes": True})
                    with self._lock:
                        self._plans.pop(sid, None)   # an older plan would now be stale
                    return
                if not destroy:
                    self._refuse_loss(sid, p.get("changes") or [])
                p.update(action="destroy" if destroy else "apply", job=job["id"], _tf=tf)
                with self._lock:
                    self._plans[sid] = p
                result = self._public_plan(p)
                self._stage(job, APPROVAL, "waiting")
                self._say(job, f"Plan ready: {_summary_text(p.get('summary'))}. Nothing changes until you approve it.")
            self._finish(job, "done", result=result)
            # Read the stacks on it again after a change, a read as deployed, or a read as partly deleted
            # (its store gone: they read as deleted, and a destroy no longer waits on them).
            if action == "apply" or (action == "status" and (result.get("deployed") or result.get("deleted"))):
                self._reread_dependents(sid)
        except (TerraformError, ApplyRefused, stacks.StackError, AdoError, JobError, OSError) as exc:
            msg = redact.text(str(exc))[:2000]
            first = msg.splitlines()[0] if msg else type(exc).__name__
            self._say(job, f"Failed: {first}")
            self._stage_failed(job)
            if action == "status":   # e.g. the state store does not exist yet: nothing is deployed there
                self._set_status(sid, {"deployed": False, "outputs": {}, "error": first[:300]})
            job["next"] = next_step(action, msg, local_state=self._local_state(sid))
            self._finish(job, "failed", error=msg)

    def _follow(self, job, run_id, until, seen=None) -> dict:
        """Poll the run until its phase is in `until`; say each new phase once and stream the
        pipeline steps' own output as it arrives (`seen` carries what was already shown)."""
        said, started = None, time.monotonic()
        seen = {} if seen is None else seen
        words = {"planning": "Azure DevOps is planning…", "applying": "Azure DevOps is applying the approved plan…"}
        while True:
            st = self.ado.stack_run(run_id)
            if st["phase"] != said and st["phase"] in words:
                self._say(job, words[st["phase"]])
            said = st["phase"]
            try:
                lines = self.ado.stream(run_id, seen)
            except AdoError:
                lines = []   # the log is a view: a failed read never stops the run
            if lines:
                with self._lock:
                    _append(job, [redact.text(l) for l in lines])
                if any(INITIALISED in l for l in lines) and any(x["name"] == INIT and x["state"] == "running" for x in job.get("stages") or []):
                    self._stage(job, INIT, "succeeded")
                    self._stage(job, PLAN, "running", st["url"])
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
            job["ado_run"] = run
            self._keep(job)
            verb = "destroy" if plan["action"] == "destroy" else "deploy"
            self._say(job, f"Approving the {verb} plan of {name} in Azure DevOps (run {run}, plan {plan['sha256'][:12]}).")
            self.ado.answer(ap, True, f"Approved in the console: plan {plan['sha256'][:12]}")
            self._stage(job, APPROVAL, "succeeded", self.ado.run_url(run))
            self._stage(job, DESTROY if plan["action"] == "destroy" else APPLY, "running", self.ado.run_url(run))
            st = self._follow(job, run, ("done",), plan["_ado"].get("seen"))
            if st["phase"] != "done":
                raise AdoError(st.get("error") or f"run {run} was {st['phase']}: {st['url']}")
            destroyed = plan["action"] == "destroy"
            self._stage(job, DESTROY if destroyed else APPLY, "succeeded")
            outs = {} if destroyed else st.get("outputs") or {}
            self._set_status(sid, {"deployed": not destroyed, "outputs": outs})
            self._say(job, "Destroyed." if destroyed else "Deployed.", *_outputs_lines(outs))
            result = {"applied": True, "destroyed": destroyed, "outputs": outs, "run": st["url"]}
            if not destroyed:
                result.update(self._deliver(job, sid, outs))
            return result
        destroy = action == "plan-destroy"
        stacks.tf_args(sid, self.env_name, self.root, self.state_root)   # inputs filled in, state derivable
        pid, pname = self.ado.pipeline(self.pipeline_path(sid))
        params = {"action": "destroy" if destroy else "apply", "environment": self.env_name,
                  "inputs": stacks.read_inputs(sid, self.env_name, self.state_root),
                  "state": dict(stacks.foundation(stacks.backend_kind(sid, self.root), self.env_name, self.state_root),
                                **({"instance": stacks.split(sid)[1]} if stacks.split(sid)[1] else {}))}
        run = self.ado.queue(pid, params)
        job["ado_run"] = run
        self._keep(job)
        self._say(job, f"Planning a {'destroy' if destroy else 'deploy'} of {name} in Azure DevOps: "
                       f"{pname}, run {run} ({self.ado.run_url(run)}).")
        seen = {}
        self._stage(job, INIT, "running", self.ado.run_url(run))
        st = self._follow(job, run, ("waiting_approval", "done"), seen)
        if st["phase"] in ("waiting_approval", "done"):
            self._stage(job, INIT, "succeeded")
            self._stage(job, PLAN, "succeeded", st["url"])
        if st["phase"] == "done":
            self._say(job, "No changes: the stack already matches its inputs.")
            self._skip_rest(job, keep=(BUILD, ONBOARD))
            result = {"action": "destroy" if destroy else "apply", "summary": {}, "changes": [], "run": st["url"], "no_changes": True}
            if not destroy:   # the infrastructure is as asked: still build, deploy and onboard the workload
                result.update(self._deliver(job, sid, (self._status.get(sid) or {}).get("outputs") or {}))
            return result
        if st["phase"] != "waiting_approval":
            raise AdoError(st.get("error") or f"run {run} was {st['phase']}: {st['url']}")
        if not destroy:
            try:
                self._refuse_loss(sid, st["changes"])
            except JobError:   # the run would otherwise wait on its approval: reject it in ADO too
                try:
                    self.ado.answer(st["approval"], False, "Refused by the console: this deploy would delete existing resources")
                except AdoError:
                    pass
                raise
        p = {"action": "destroy" if destroy else "apply", "sha256": st["sha256"], "summary": st["summary"],
             "changes": st["changes"], "destructive": st["destructive"], "confirmation_phrase": sid,
             "job": job["id"], "_ado": {"run": run, "approval": st["approval"], "seen": seen}}
        with self._lock:
            self._plans[sid] = p
        self._stage(job, APPROVAL, "waiting", st["url"])
        self._say(job, f"Plan ready: {_summary_text(p['summary'])}. Nothing changes until you approve it.")
        return self._public_plan(p)

    def _refuse_loss(self, sid, changes) -> None:
        """A deploy only adds or updates. Refused: a plan that deletes or replaces anything in a deployment
        that is not deployed yet (a new one must not touch what exists: a wrong state file would read as
        'replace the other app'), and any deploy that deletes or replaces a resource group. Removing
        resources is Destroy, with its own typed confirmation."""
        lost = [c["address"] for c in changes if c.get("action") in ("delete", "replace")]
        with self._lock:
            new = not (self._status.get(sid) or {}).get("deployed")
        rgs = [a for a in lost if a.split("[")[0].rsplit(".", 1)[0].endswith(("azurerm_resource_group", "google_project"))]
        if lost and (new or rgs):
            what = rgs or lost
            raise JobError(409, f"Refused: this deploy would delete or replace {', '.join(what[:5])}"
                           f"{f' and {len(what) - 5} more' if len(what) > 5 else ''}. "
                           + ("A new deployment only creates resources: its state must be its own. Nothing was changed."
                              if new else "Deleting a resource group is Destroy, never Deploy. Nothing was changed."))

    def _reread_dependents(self, sid) -> None:
        """A stack that was just deployed or destroyed changes what the stacks on it can read: read them again."""
        for x in stacks.all_ids(self.env_name, self.state_root):
            if sid in stacks.upstreams(x, self.root):
                try:
                    self.start(x, "status")
                except (JobError, stacks.StackError):
                    continue

    def refresh(self, ids) -> list:
        """Refresh: forget what is known of these stacks' state and read it again (stacks with a running
        job or a pending plan keep theirs)."""
        with self._lock:
            for sid in ids:
                if sid not in self._plans and not any(j["stack"] == sid and j["state"] == "running" for j in self._jobs.values()):
                    self._status.pop(sid, None)
        return self.refresh_all()

    def refresh_all(self) -> list:
        """Read every stack's state in the background, so the list knows what is deployed without a
        Check button. Foundations first: a stack is read only once its foundation reads as deployed
        (that read starts it), so nothing is touched before what it stands on exists. Skips stacks with
        inputs still missing, a known status, or a running job."""
        started = []
        for sid in stacks.all_ids(self.env_name, self.state_root):
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

    def _store_gone(self, sid) -> str | None:
        """Why this stack's state store is known to be gone (the cloud answered not-found), else None.
        A foundation's store is the one it creates; a workload's is its foundation's."""
        kind = stacks.backend_kind(sid, self.root) or {v: k for k, v in stacks.FOUNDATION.items()}.get(sid)
        store = state_store(kind, stacks.foundation(kind, self.env_name, self.state_root)) if kind else None
        if not store or self.probe(kind, store) != "missing":
            return None
        return f"its state store, {store['label']}, no longer exists"

    def _local_state(self, sid) -> bool:
        """The stack keeps its state in a local file (a foundation) and that file exists."""
        return stacks.backend_kind(sid, self.root) is None and (stacks.stack_dir(sid, self.root) / "terraform.tfstate").is_file()

    def _forget(self, job, meta) -> dict:
        """Move a local state file (and its backup) aside, out of the stack, after its resources were
        deleted outside the console. Nothing in the cloud is called; the file is kept to restore."""
        sid = job["stack"]
        d = stacks.stack_dir(sid, self.root)
        keep = stacks.Path(self.state_root) / self.env_name / "forgotten"
        keep.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self._say(job, f"Forgetting the Terraform state of {meta['title']} ({sid}): nothing in the cloud is touched.")
        for name, suffix in (("terraform.tfstate", ".tfstate"), ("terraform.tfstate.backup", ".tfstate.backup")):
            src = d / name
            if src.is_file():
                dst = keep / f"{sid}-{stamp}{suffix}"
                src.replace(dst)
                self._say(job, f"Moved {name} aside.", f"  kept as {dst.name} in .console/setup/{self.env_name}/forgotten")
        self._set_status(sid, {"deployed": False, "outputs": {}, "deleted": "State forgotten: its resources were deleted outside the console."})
        self._say(job, "Done: the stack shows as not deployed. Deploy builds it again from nothing.")
        return {"forgotten": True, "deployed": False, "outputs": {}}

    def _set_status(self, sid, status):
        with self._lock:
            self._status[sid] = dict(status, checked=time.time())

    def _finish(self, job, state, result=None, error=None):
        with self._lock:
            job.update(state=state, result=result, error=error, finished=time.time())
        self._keep(job)
        res = result if isinstance(result, dict) else {}
        if state == "done" and (res.get("destroyed") or res.get("already_deleted") or res.get("forgotten")
                                or (job["action"] == "status" and res.get("deployed") is False)):
            self.archive_stack(job["stack"], res.get("deleted") or res.get("message")
                               or ("Destroyed" if res.get("destroyed") else "Not deployed"))


def _meta(sid) -> dict | None:
    try:
        return stacks.get(str(sid))
    except stacks.StackError:
        return None


def _name(sid: str) -> str:
    m = stacks.get(sid)
    return f"{m['kind']} · {m['group']} · {m['title']} ({sid})"


def state_store(kind: str, found: dict) -> dict | None:
    """Where the foundation of `kind` keeps state (stacks.backend_config's naming), for asking the cloud."""
    if kind == "gcs" and found.get("gcp_project_id"):
        bucket = f"{found['gcp_project_id']}-tfstate"
        return {"bucket": bucket, "label": f"Cloud Storage bucket {bucket}"}
    if kind == "azurerm" and found.get("azure_subscription_id") and found.get("ado_org") and found.get("state_resource_group"):
        account = ("st" + found["ado_org"].replace("-", "") + "tfstate")[:24]
        return {"account": account, "resource_group": found["state_resource_group"], "subscription": found["azure_subscription_id"],
                "label": f"storage account {account} (resource group {found['state_resource_group']})"}
    return None


def probe_store(kind: str, store: dict) -> str:
    """Ask the cloud whether a state store exists: "missing" only on a not-found answer; any other
    failure (no sign-in, no access, no CLI) is "unknown", which never counts as deleted."""
    if kind == "gcs":
        argv = ["gcloud", "storage", "buckets", "describe", f"gs://{store['bucket']}", "--format=value(name)"]
    else:
        argv = ["az", "storage", "account", "show", "-n", store["account"], "-g", store["resource_group"],
                "--subscription", store["subscription"], "--query", "name", "-o", "tsv"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if r.returncode == 0:
        return "present"
    return "missing" if NOT_FOUND.search(r.stderr or "") else "unknown"


def _check_ado_approval(plan, approval) -> None:
    """The same gate as a local plan: the approval names the plan shown; deleting needs the name typed."""
    if not isinstance(approval, dict) or approval.get("plan_sha256") != plan["sha256"]:
        raise ApplyRefused("plan_hash_mismatch", "the approval is for a different plan; review the current plan")
    if plan.get("destructive") and approval.get("typed_confirmation") != plan["confirmation_phrase"]:
        raise ApplyRefused("typed_confirmation_required",
                           f"this plan deletes or replaces {len(plan['destructive'])} resource(s); "
                           f"type the stack name '{plan['confirmation_phrase']}' to confirm")


_UNREADABLE = re.compile(r"(Error 403|Error 404|does not have permission|AuthorizationFailed|was not found|could not be found"
                         r"|ResourceGroupNotFound|NotFound|notFound|Missing JFrog Access Token|status: 401|status: 403)")
_RETRY = {"plan-apply": "Deploy again", "plan-destroy": "Destroy again", "apply": "Plan again", "status": "Check again"}


def next_step(action: str, error: str, local_state: bool) -> dict:
    """What to do about a failed run, in words, with the actions that fix it (first = most likely)."""
    err = error or ""
    retry = {"action": "plan-apply" if action == "apply" else action, "label": _RETRY.get(action, "Try again")}
    check = {"action": "status", "label": "Check status"}
    if action in ("plan-destroy", "plan-apply", "status") and _UNREADABLE.search(err):
        if local_state:
            return {"cause": "Terraform cannot read resources its state lists: they, or the identity that reads them, "
                             "were deleted outside the console. If they are gone, forget the state: nothing in the cloud "
                             "is touched and the file is kept.",
                    "actions": [{"action": "forget", "label": "Forget state", "confirm": True}, check, retry]}
        return {"cause": "Terraform cannot read resources its state lists: they, or the identity that reads them, were "
                         "deleted outside the console, or the foundation's identity lost access. Check the foundation, "
                         "then try again.",
                "actions": [check, retry]}
    return {"cause": "The run stopped on the error above. Fix what it names (inputs, sign-in, quota), then try again.",
            "actions": [retry, check] if action != "status" else [check]}


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
