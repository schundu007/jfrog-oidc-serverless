"""One stack's change through the Setup console's own gates (Jobs): a plan, then an apply of exactly
that plan, approved by its sha256 with the stack name typed. Shared by costs (infrastructure) and oidc
(JFrog OIDC); neither imports the other."""

from __future__ import annotations

import threading
import time

from console import redact

from . import stacks

POLL = 5


class RunError(RuntimeError):
    pass


class Runner:
    """Plans, approves and applies one stack at a time; stacks with a stack-setup pipeline run in ADO."""
    error = RunError

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
            raise self.error(f"{action} failed: {(j.get('error') or '').splitlines()[0][:400] if j.get('error') else 'no detail'}")
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

    def deployed(self, sid) -> bool:
        return bool(self.read(sid).get("deployed"))
