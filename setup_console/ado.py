"""Azure DevOps pipelines for Setup: every stack change runs in ADO, never on this machine.

A stack's stack-setup pipeline plans, waits on its ADO environment's approval, then applies
exactly that plan file. This client queues it, reads the plan the run made (from the plan task's
log), and answers the approval: the console's Approve (or Yes) is the approval, given by the
signed-in az account, which the foundation made the environment's approver. The az token is
fetched as a sensitive call and only ever sent as a header.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from console import redact

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
API = "api-version=7.1"
TOKEN_SECONDS = 30 * 60

_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ?")
_CHANGE = re.compile(r"^\s*# (\S+) (will be created|will be destroyed|must be replaced|will be updated in-place|"
                     r"will be read during apply|will be imported)")
_ACTION = {"will be created": "create", "will be destroyed": "delete", "must be replaced": "replace",
           "will be updated in-place": "update", "will be imported": "import"}
_OUTPUT = re.compile(r"^([A-Za-z0-9_]+) = (.*)$")


class AdoError(RuntimeError):
    pass


def clean(log_text: str) -> list:
    """A task log as plain lines: no timestamps, no ##[section] markers."""
    out = []
    for line in log_text.splitlines():
        line = _STAMP.sub("", line)
        if not line.startswith("##["):
            out.append(line)
    return out


def plan_changes(lines: list) -> list:
    return [{"action": _ACTION[m.group(2)], "address": m.group(1)}
            for m in map(_CHANGE.match, lines) if m and m.group(2) in _ACTION]


def outputs(lines: list) -> dict:
    """`terraform output` after the apply: top-level scalars (maps and lists are skipped)."""
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("Apply complete!"))
    except StopIteration:
        return {}
    out = {}
    for line in lines[start + 1:]:
        m = _OUTPUT.match(line)
        if not m or m.group(2) in ("{", "[", "tomap({", "tolist(["):
            continue
        v = m.group(2)
        out[m.group(1)] = json.loads(v) if v.startswith('"') else v
    return out


class Ado:
    def __init__(self, sh, organization: str, project: str, branch: str = "main"):
        self.sh, self.branch = sh, branch
        self.org = organization
        self.base = f"https://dev.azure.com/{organization}/{project}/_apis"
        self.web = f"https://dev.azure.com/{organization}/{project}"
        self._token, self._token_at = "", 0.0
        self._defs = None

    # -- transport --------------------------------------------------------------------------

    def _auth(self) -> dict:
        if not self._token or time.monotonic() - self._token_at > TOKEN_SECONDS:
            res = self.sh.run(["az", "account", "get-access-token", "--resource", ADO_RESOURCE,
                               "--query", "accessToken", "-o", "tsv"], sensitive=True)
            if not res.ok or not res.stdout.strip():
                raise AdoError("no Azure DevOps sign-in: run `az login` with the ADO account")
            self._token, self._token_at = res.stdout.strip(), time.monotonic()
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, body=None, text=False):
        sep = "&" if "?" in path else "?"
        headers = self._auth()
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        res = self.sh.http(method, f"{self.base}{path}{sep}{API}", headers=headers, body=data, timeout=60)
        if not res.ok:
            msg = (res.json({}) or {}).get("message") if res.body.startswith("{") else ""
            raise AdoError(f"Azure DevOps answered {res.status or res.error} to {method} {path.split('?')[0]}"
                           + (f": {redact.text(msg)[:300]}" if msg else ""))
        return res.body if text else res.json({})

    def users(self) -> list:
        """The organisation's users by email (read-only), for the approver picker: [{id, name}]."""
        res = self.sh.http("GET", f"https://vsaex.dev.azure.com/{self.org}/_apis/userentitlements?top=500&api-version=7.1-preview.3",
                           headers=self._auth(), timeout=60)
        if not res.ok:
            raise AdoError(f"Azure DevOps answered {res.status or res.error} to the user list")
        items = (res.json({}) or {}).get("items") or (res.json({}) or {}).get("members") or []
        out = {}
        for it in items:
            u = it.get("user") or {}
            email = u.get("mailAddress") or u.get("principalName")
            if email and "@" in email:
                out[email] = u.get("displayName") or email
        return [{"id": e, "name": n} for e, n in out.items()]

    # -- pipelines --------------------------------------------------------------------------

    def pipeline(self, yaml_path: str) -> tuple:
        """(id, name) of the pipeline whose YAML is yaml_path (repo-relative)."""
        if self._defs is None:
            self._defs = self._call("GET", "/build/definitions?includeAllProperties=true").get("value") or []
        want = yaml_path.lstrip("/")
        for d in self._defs:
            if str((d.get("process") or {}).get("yamlFilename", "")).lstrip("/") == want:
                return d["id"], d["name"]
        self._defs = None   # registered since: look again next time
        raise AdoError(f"no Azure DevOps pipeline runs {want}: register it first")

    def queue(self, pipeline_id: int, params: dict) -> int:
        """Queue a run on the branch; object parameters go as JSON text (ADO parses them)."""
        tp = {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
        body = {"resources": {"repositories": {"self": {"refName": f"refs/heads/{self.branch}"}}}, "templateParameters": tp}
        return int(self._call("POST", f"/pipelines/{pipeline_id}/runs", body)["id"])

    def run_url(self, run_id: int) -> str:
        return f"{self.web}/_build/results?buildId={run_id}"

    def _timeline(self, run_id: int) -> list:
        return self._call("GET", f"/build/builds/{run_id}/timeline").get("records") or []

    def _log(self, run_id: int, rec) -> list:
        log_id = ((rec or {}).get("log") or {}).get("id")
        if not log_id:
            return []
        text = self._call("GET", f"/build/builds/{run_id}/logs/{log_id}", text=True)
        try:   # asked for JSON, ADO sends the lines as {"count": n, "value": [...]}
            data = json.loads(text)
            text = "\n".join(data["value"]) if isinstance(data, dict) and isinstance(data.get("value"), list) else text
        except ValueError:
            pass
        return clean(text)

    def warnings(self, run_id: int) -> list:
        return [i["message"] for r in self._timeline(run_id) for i in (r.get("issues") or []) if i.get("type") == "warning"]

    def build(self, run_id: int) -> dict:
        """A plain run (build-deploy): its state, result, errors and warnings."""
        recs = self._timeline(run_id)
        b = self._call("GET", f"/build/builds/{run_id}")
        issues = [i for r in recs for i in (r.get("issues") or [])]
        return {"state": b.get("status"), "result": b.get("result"),
                "errors": [i["message"] for i in issues if i.get("type") == "error" and not i["message"].startswith("Bash exited")],
                "warnings": [i["message"] for i in issues if i.get("type") == "warning"]}

    def pending_approval(self, run_id: int) -> str | None:
        for a in self._call("GET", "/pipelines/approvals?state=pending").get("value") or []:
            if ((a.get("pipeline") or {}).get("owner") or {}).get("id") == run_id:
                return a["id"]
        return None

    def answer(self, approval_id: str, approve: bool, comment: str) -> None:
        self._call("PATCH", "/pipelines/approvals",
                   [{"approvalId": approval_id, "status": "approved" if approve else "rejected", "comment": comment}])

    def stack_run(self, run_id: int) -> dict:
        """Where a stack-setup run is. phase: planning | waiting_approval | applying | done | declined | failed.
        With the plan read: changes, summary, destructive, sha256 (of the plan shown); when applied: outputs."""
        recs = self._timeline(run_id)
        stages = {r.get("identifier"): r for r in recs if r.get("type") == "Stage"}
        tasks = [r for r in recs if r.get("type") == "Task"]
        plan_task = next((t for t in tasks if t.get("name", "").startswith("terraform") and "plan" in t["name"]), None)
        apply_task = next((t for t in tasks if t.get("name", "").startswith("terraform apply") and "plan" not in t["name"]), None)
        plan, apply = stages.get("plan") or {}, stages.get("apply") or {}
        out = {"run": run_id, "url": self.run_url(run_id)}
        if plan.get("state") != "completed":
            return dict(out, phase="planning")
        lines = self._log(run_id, plan_task)
        if plan.get("result") not in ("succeeded", "succeededWithIssues"):
            return dict(out, phase="failed", error=_why(lines, plan_task) or f"the plan stage {plan.get('result')}")
        changes = plan_changes(lines)
        summary = {}
        for c in changes:
            summary[c["action"]] = summary.get(c["action"], 0) + 1
        shown = "\n".join(l for l in lines if _CHANGE.match(l) or l.startswith(("Plan:", "No changes")))
        out.update(changes=changes, summary=summary, sha256=hashlib.sha256(f"{run_id}\n{shown}".encode()).hexdigest(),
                   destructive=[c["address"] for c in changes if c["action"] in ("delete", "replace")])
        if apply.get("state") == "completed":
            if apply.get("result") in ("succeeded", "succeededWithIssues"):
                return dict(out, phase="done", outputs=outputs(self._log(run_id, apply_task)))
            if apply.get("result") == "skipped":
                return dict(out, phase="done" if not changes else "declined")
            if apply.get("result") in ("canceled", "rejected"):
                return dict(out, phase="declined")
            return dict(out, phase="failed", error=_why(self._log(run_id, apply_task), apply_task)
                        or f"the apply stage {apply.get('result')}")
        # A pipeline not yet permitted to use the environment waits here, before its approval opens.
        if any(r.get("type") == "Checkpoint.Authorization" and r.get("state") != "completed" for r in recs):
            return dict(out, phase="authorizing",
                        error="the pipeline is not permitted to use its ADO environment yet: deploy the foundation again")
        # The run's own approval checkpoint (open = inProgress): its record id is the approval id.
        approval = next((r["id"] for r in recs if r.get("type") == "Checkpoint.Approval" and r.get("state") == "inProgress"), None)
        if approval:
            return dict(out, phase="waiting_approval", approval=approval)
        return dict(out, phase="applying" if apply.get("state") == "inProgress" else "planning")


def _why(lines: list, task) -> str:
    """The Terraform error lines of a failed task, else its issues."""
    for i, l in enumerate(lines):
        if l.startswith("Error:"):
            return redact.text(" ".join(x.strip() for x in lines[i:i + 6] if x.strip()))[:600]
    issues = [i["message"] for i in ((task or {}).get("issues") or []) if not i["message"].startswith("Bash exited")]
    return redact.text("; ".join(issues))[:600]
