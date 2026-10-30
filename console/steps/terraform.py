"""Terraform operations on a rendered workspace (spec §4.3) with the apply gates.

plan():  init, `plan -out=plan.bin`, `show -json plan.bin` → summary of
         resource_changes, the destructive addresses, and the plan file's sha256.
apply(): runs `apply plan.bin` and nothing else, only if
         - the operator approved this plan (approval.plan_sha256 == plan.sha256),
         - plan.bin on disk still hashes to that value, and
         - for any delete/replace, approval.typed_confirmation equals the
           workspace name shown in the UI.
         Every refusal happens before a subprocess starts. A plan file is
         single-use: it is removed after apply.

JFROG_ACCESS_TOKEN reaches terraform through its environment at call time;
it is never an argument, a variable or a file. `show -json` and `output -json`
run as sensitive calls (not recorded); only the summary leaves this module and
sensitive outputs are replaced.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..shell import Shell
from .tfgen import terraform_lock

PLAN_FILE = "plan.bin"
TIMEOUTS = {"init": 600, "validate": 300, "plan": 1800, "show": 300, "apply": 3600, "output": 120}


class TerraformError(RuntimeError):
    pass


class ApplyRefused(PermissionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def summarise(resource_changes: list) -> dict:
    summary = {"import": 0, "create": 0, "update": 0, "replace": 0, "delete": 0, "read": 0, "no-op": 0}
    changes, destructive = [], []
    for rc in resource_changes or []:
        actions = list((rc.get("change") or {}).get("actions") or [])
        if "delete" in actions and "create" in actions:
            action = "replace"
        elif actions in (["create"], ["update"], ["delete"], ["read"], ["no-op"]):
            action = actions[0]
        else:
            action = "+".join(actions) or "no-op"
        if action == "no-op" and (rc.get("change") or {}).get("importing"):
            action = "import"   # adopted into state as it is; nothing changes remotely
        summary[action] = summary.get(action, 0) + 1
        if action in ("delete", "replace"):
            destructive.append(rc.get("address"))
        if action != "no-op":
            changes.append({"address": rc.get("address"), "type": rc.get("type"), "action": action})
    return {"summary": summary, "changes": changes, "destructive": destructive}


class Terraform:
    def __init__(self, sh: Shell, ws_dir, env=None, on_wait=None, lock_timeout: float = 3600.0):
        self.sh = sh
        self.ws = Path(ws_dir)
        self.env = os.environ if env is None else env
        self.on_wait = on_wait
        self.lock_timeout = lock_timeout

    # -- plumbing ----------------------------------------------------------------

    def _extra_env(self) -> dict:
        extra = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        token = self.env.get("JFROG_ACCESS_TOKEN")
        if token:
            extra["JFROG_ACCESS_TOKEN"] = token
        return extra

    def _run(self, *args, ok=(0,), sensitive=False):
        res = self.sh.run(["terraform", f"-chdir={self.ws}", *args], timeout=TIMEOUTS.get(args[0], 600),
                          sensitive=sensitive, extra_env=self._extra_env())
        if res.rc not in ok:
            raise TerraformError(f"terraform {args[0]} failed (exit {res.rc}): {res.error_text(1500)}")
        return res

    def _lock(self):
        return terraform_lock(self.ws, timeout=self.lock_timeout, on_wait=self.on_wait)

    # -- operations -------------------------------------------------------------------

    def init(self):
        return self._run("init", "-input=false", "-no-color")

    def validate(self) -> dict:
        res = self._run("validate", "-json", "-no-color", ok=(0, 1))
        out = res.json() or {}
        return {"valid": bool(out.get("valid")),
                "diagnostics": [{"severity": d.get("severity"), "summary": d.get("summary"), "detail": d.get("detail")}
                                for d in out.get("diagnostics") or []]}

    def plan(self) -> dict:
        with self._lock():
            (self.ws / PLAN_FILE).unlink(missing_ok=True)
            self.init()
            self._run("plan", f"-out={PLAN_FILE}", "-input=false", "-lock-timeout=60s", "-no-color",
                      "-detailed-exitcode", ok=(0, 2))
            data = self._run("show", "-json", "-no-color", PLAN_FILE, sensitive=True).json()
            if not isinstance(data, dict):
                raise TerraformError("terraform show -json did not return JSON")
            out = summarise(data.get("resource_changes"))
            out["sha256"] = _sha256(self.ws / PLAN_FILE)
            out["workspace"] = self.ws.name
            out["confirmation_phrase"] = self.ws.name if out["destructive"] else None
            return out

    def check_approval(self, plan: dict, approval) -> None:
        """Raise ApplyRefused unless this exact plan was approved. No side effects."""
        if not isinstance(approval, dict) or not approval.get("plan_sha256"):
            raise ApplyRefused("not_approved", "apply needs the operator's approval of this plan")
        if approval["plan_sha256"] != plan.get("sha256"):
            raise ApplyRefused("plan_hash_mismatch", "the approval is for a different plan; review the current plan")
        path = self.ws / PLAN_FILE
        if not path.is_file() or _sha256(path) != plan["sha256"]:
            raise ApplyRefused("plan_file_changed", "the plan file changed or was used since it was approved; plan again")
        if plan.get("destructive") and approval.get("typed_confirmation") != plan.get("confirmation_phrase"):
            raise ApplyRefused("typed_confirmation_required",
                               f"this plan deletes or replaces {len(plan['destructive'])} resource(s); "
                               f"type the workspace name '{plan['confirmation_phrase']}' to confirm")

    def apply(self, plan: dict, approval) -> dict:
        self.check_approval(plan, approval)
        with self._lock():
            self.check_approval(plan, approval)      # again, now that nobody else can touch the workspace
            try:
                self._run("apply", "-input=false", "-lock-timeout=60s", "-no-color", PLAN_FILE)
            finally:
                (self.ws / PLAN_FILE).unlink(missing_ok=True)
            return {"applied": True, "sha256": plan["sha256"], "outputs": self.outputs()}

    def outputs(self) -> dict:
        data = self._run("output", "-json", "-no-color", sensitive=True).json() or {}
        return {k: ("[sensitive]" if v.get("sensitive") else v.get("value")) for k, v in data.items() if isinstance(v, dict)}
