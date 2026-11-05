"""Azure DevOps pipelines for Setup: every stack change runs in ADO, never on this machine.

A stack's stack-setup pipeline plans, waits on its ADO environment's approval, then applies
exactly that plan file. This client queues it, reads the plan the run made (from the plan task's
log), and answers the approval: the console's Approve (or Yes) is the approval, given by the
signed-in az account, which the foundation made the environment's approver. The az token is
fetched as a sensitive call and only ever sent as a header.
"""

from __future__ import annotations

from urllib.parse import quote
import hashlib
import json
import re
import time

from console import order, redact

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
API = "api-version=7.1"
TOKEN_SECONDS = 30 * 60

_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ?")
_CHANGE = re.compile(r"^\s*# (\S+) (will be created|will be destroyed|must be replaced|will be updated in-place|"
                     r"will be read during apply|will be imported)")
_ACTION = {"will be created": "create", "will be destroyed": "delete", "must be replaced": "replace",
           "will be updated in-place": "update", "will be imported": "import"}
_OUTPUT = re.compile(r"^([A-Za-z0-9_]+) = (.*)$")
# Agent chatter around a script step: the command it ran and the script it generated (never shown).
_NOISE = re.compile(r"^(\[command\]|=+ ?(Starting|Finishing)?:? ?Command Output|Generating script\.|"
                    r"Script contents:|Formatted command:|========|"
                    r"(Task|Description|Version|Author|Help) +: |\[Host\] |Setting AZURE_CONFIG_DIR|Setting active cloud)")
# Agent housekeeping steps: nothing of the stack in them.
_CHORES = re.compile(r"^(Initialize job|Checkout |Post-job: |Finalize Job|PublishPipelineArtifact|Download$|"
                     r"DownloadPipelineArtifact|Publish|Report build status)")
# Where Terraform's own output starts in a terraform step (the sign-in before it is not shown).
_TF_START = re.compile(r"^(Initializing |Terraform |Error|╷|No changes|Plan:|Apply complete)")


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


BEAT_SECONDS = 30


class Ado:
    clock = staticmethod(time.monotonic)

    def __init__(self, sh, organization: str, project: str, branch: str = "main", repo: str = ""):
        self.sh = sh
        self.retarget(organization, project, branch, repo)

    def retarget(self, organization: str, project: str, branch: str = "main", repo: str = "") -> None:
        """Point at another organization or project (Settings): the sign-in and the pipeline list start over."""
        self.branch, self.repo, self.org = branch or "main", repo, organization
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
                raise AdoError("no Azure DevOps sign-in: sign in to Azure on Settings with the ADO account")
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
        if res.status == 401:
            # az can return a cached token close to expiry: get a fresh one and try once more.
            self._token = ""
            headers.update(self._auth())
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

    def in_flight(self) -> list:
        """Queued or running builds, each with its cloud and step (console/order.py)."""
        if self._defs is None:
            self._defs = self._call("GET", "/build/definitions?includeAllProperties=true").get("value") or []
        yaml = {d["id"]: str((d.get("process") or {}).get("yamlFilename") or "") for d in self._defs}
        out = []
        for b in self._call("GET", "/build/builds?statusFilter=notStarted,inProgress,cancelling&$top=100").get("value") or []:
            y = yaml.get((b.get("definition") or {}).get("id"), "")
            out.append({"id": b.get("id"), "name": (b.get("definition") or {}).get("name"), "cloud": order.cloud_of(y),
                        "step": order.step_of(y), "state": {"notStarted": "queued"}.get(b.get("status"), b.get("status"))})
        return out

    def queue(self, pipeline_id: int, params: dict) -> int:
        """Queue a run on the branch; object parameters go as JSON text (ADO parses them).
        Refused while another pipeline of the same cloud is queued or running (the step order)."""
        y = next((str((d.get("process") or {}).get("yamlFilename") or "") for d in self._defs or []
                  if d.get("id") == pipeline_id), "")
        cloud = order.cloud_of(y)
        if cloud:
            b = order.blocker(self.in_flight(), cloud)
            if b:
                raise AdoError(f"waiting on {order.describe(b)}; start this when it has finished")
        tp = {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
        body = {"resources": {"repositories": {"self": {"refName": f"refs/heads/{self.branch}"}}}, "templateParameters": tp}
        return int(self._call("POST", f"/pipelines/{pipeline_id}/runs", body)["id"])

    def source_url(self, rel: str) -> str | None:
        """The ADO web page of a repo path (a folder or file), for links; no network call."""
        if not self.repo:
            return None
        return f"{self.web}/_git/{quote(self.repo)}?path=/{quote(rel.lstrip('/'))}&version=GB{quote(self.branch)}"

    def pipeline_url(self, yaml_path: str) -> str:
        """The ADO page of the pipeline that runs yaml_path; the pipelines list until its id is known.
        Uses only the definitions already read: never a network call."""
        want = yaml_path.lstrip("/")
        for d in self._defs or []:
            if str((d.get("process") or {}).get("yamlFilename", "")).lstrip("/") == want:
                return f"{self.web}/_build?definitionId={int(d['id'])}"
        return f"{self.web}/_build"

    def run_url(self, run_id: int) -> str:
        return f"{self.web}/_build/results?buildId={run_id}"

    def _timeline(self, run_id: int) -> list:
        return self._call("GET", f"/build/builds/{run_id}/timeline").get("records") or []

    def _raw(self, run_id: int, log_id, start: int = 1) -> list:
        """A step log's raw lines from line `start` (1-based) on."""
        q = f"?startLine={start}" if start > 1 else ""
        text = self._call("GET", f"/build/builds/{run_id}/logs/{log_id}{q}", text=True)
        try:   # asked for JSON, ADO sends the lines as {"count": n, "value": [...]}
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("value"), list):
                return [str(l) for l in data["value"]]
        except ValueError:
            pass
        return text.splitlines()

    def _log(self, run_id: int, rec) -> list:
        log_id = ((rec or {}).get("log") or {}).get("id")
        if not log_id:
            return []
        return clean("\n".join(self._raw(run_id, log_id)))

    def stream(self, run_id: int, seen: dict) -> list:
        """The run's step output written since the last call, each step under its name once.
        `seen` (kept by the caller across calls: log id -> lines read, -1 once a step is read to its end)
        makes every call return only what is new. Lines are redacted; commands and scripts are left out."""
        # ADO attaches a step's log only when the step ends: a running step is named when it starts and
        # gets a "still running" line every BEAT_SECONDS, so the log never sits silent.
        steps = [r for r in self._timeline(run_id) if r.get("type") == "Task"
                 and r.get("state") in ("inProgress", "completed") and r.get("result") != "skipped"
                 and not _CHORES.match(r.get("name") or "")]
        steps.sort(key=lambda r: (r.get("startTime") or "9", r.get("order") or 0))
        out, now = [], self.clock()
        for r in steps:
            key = "st:" + str(r.get("id") or r.get("name"))
            if key not in seen:
                seen[key] = now
                out.append(f"── {r.get('name') or 'step'}")
            if not (r.get("log") or {}).get("id"):
                if r.get("state") == "inProgress" and now - seen[key] >= BEAT_SECONDS * (seen.get("b:" + key, 0) + 1):
                    seen["b:" + key] = seen.get("b:" + key, 0) + 1
                    secs = int(now - seen[key])
                    out.append(f"   still running ({secs // 60}m {secs % 60:02d}s)" if secs >= 60 else f"   still running ({secs}s)")
                continue
            lid = str(r["log"]["id"])
            n = seen.get(lid, 0)
            if n < 0:
                continue
            try:
                raw = self._raw(run_id, r["log"]["id"], n + 1)
            except AdoError:
                continue   # not uploaded yet: next time
            shown = []
            for l in clean("\n".join(raw)):   # a script step prints its script first: never shown
                if l.startswith(("Script contents:", "Generating script.")):
                    seen["sc:" + lid] = True
                elif "Starting Command Output" in l:
                    seen["sc:" + lid] = False
                elif not seen.get("sc:" + lid) and not _NOISE.match(l):
                    shown.append(l)
            if (r.get("name") or "").startswith("terraform") and not seen.get("tf:" + lid):
                at = next((i for i, l in enumerate(shown) if _TF_START.match(l)), None)
                shown = [] if at is None else shown[at:]
                seen["tf:" + lid] = at is not None
            for l in shown:   # blank lines: at most one in a row, none right under a step name
                if l.strip() or (out and out[-1].strip() and not out[-1].startswith("── ")):
                    out.append(redact.text(l))
            seen[lid] = -1 if r.get("state") == "completed" else n + len(raw)
        return out

    def run_for(self, yaml_path: str, commit: str) -> int | None:
        """The run of that pipeline its CI trigger started for a commit (a push to the branch), or None yet."""
        pid, _ = self.pipeline(yaml_path)
        for b in self._call("GET", f"/build/builds?definitions={pid}&$top=20&queryOrder=queueTimeDescending").get("value") or []:
            if str(b.get("sourceVersion") or "") == commit:
                return int(b["id"])
        return None

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
        try:
            self._call("PATCH", "/pipelines/approvals",
                       [{"approvalId": approval_id, "status": "approved" if approve else "rejected", "comment": comment}])
        except AdoError as exc:
            # Already answered (e.g. in ADO itself): following the run says whether it was approved.
            if "already in completed state" not in str(exc):
                raise

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
