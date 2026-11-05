"""The steps run in order, one at a time per cloud.

    1 Pipeline identity · 2 JFrog trust · 3 Infrastructure · 4 Build and deploy · 5 Onboard · 6 Prove / Verify

A step starts only when nothing for its cloud is still queued or running in Azure DevOps,
and when the step it needs has succeeded: Prove and Verify need the cloud's last Onboard
run to have succeeded. Every refusal names the step it waits on.

The pipelines are told apart by their YAML path (pipelines/<area>/<cloud>/<stack>/<file>.yml
and pipelines/<cloud>-workload-onboard.yml), so a new stack needs nothing here.
"""

from __future__ import annotations

from urllib.parse import quote

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"   # Azure DevOps
API = "api-version=7.1"
CLOUDS = ("azure", "gcp", "openshift")
NAMES = {1: "Pipeline identity", 2: "JFrog trust", 3: "Infrastructure", 4: "Build and deploy", 5: "Onboard", 6: "Prove / Verify"}


# The steps of a Deliver run (runner.DELIVERY_STEPS): the next step the console offers when Onboard is missing.
DELIVER_STEPS = ["preflight", "discover", "simulate", "publish"]
DELIVER = {"label": "Deliver now", "steps": DELIVER_STEPS}


class OrderError(RuntimeError):
    """A step that may not start yet. `next` says what to do: text, and an action or a link to follow."""

    def __init__(self, message: str, next: dict | None = None):
        super().__init__(message)
        self.next = next


def cloud_of(yaml_path: str) -> str | None:
    parts = str(yaml_path or "").lstrip("/").split("/")
    for p in parts[:-1]:
        if p in CLOUDS:
            return p
    base = parts[-1]
    return next((c for c in CLOUDS if base.startswith(f"{c}-")), None)


def step_of(yaml_path: str) -> int | None:
    parts = str(yaml_path or "").lstrip("/").split("/")
    base = parts[-1]
    if base.endswith("-workload-onboard.yml"):
        return 5
    if base == "build-deploy.yml":
        return 4
    if base == "stack-setup.yml":
        return 2 if "oidc" in parts[:-1] else 3
    return None


def describe(run: dict) -> str:
    step = run.get("step")
    what = f"step {step} ({NAMES[step]})" if step in NAMES else "a pipeline"
    return f"{what}: {run.get('name')} run {run.get('id')} is {run.get('state')}"


def blocker(active: list, cloud: str) -> dict | None:
    """The first queued or running pipeline of this cloud, or None."""
    mine = sorted((r for r in active if r.get("cloud") == cloud), key=lambda r: (r.get("step") or 9, r.get("id") or 0))
    return mine[0] if mine else None


def check(steps, cloud: str, active: list, last_onboard: dict | None) -> None:
    """Raise OrderError when the console steps may not start now. Pure: the caller reads ADO."""
    steps = set(steps)
    if not steps & {"publish", "verify", "prove"}:
        return
    b = blocker(active, cloud)
    if b:
        raise OrderError(f"waiting on {describe(b)}; start this when it has finished",
                         next={"text": f"Let {b.get('name')} run {b.get('id')} finish, then start this again.",
                               "url": b.get("url")})
    if steps & {"verify", "prove"} and "publish" not in steps:
        if not last_onboard:
            raise OrderError("waiting on step 5 (Onboard): this cloud has not been onboarded yet; deliver first",
                             next={"text": "Deliver: it runs Onboard, which applies the workspace and wires the app. "
                                           "When Onboard succeeds, start this again.", "action": DELIVER})
        if last_onboard.get("result") != "succeeded":
            raise OrderError(f"waiting on step 5 (Onboard): its last run {last_onboard.get('id')} "
                             f"{last_onboard.get('result') or 'did not finish'}; fix it and deliver again",
                             next={"text": f"Open Onboard run {last_onboard.get('id')} for its error, fix that, then "
                                           "Deliver again: it runs Onboard once more.",
                                   "url": last_onboard.get("url"), "action": DELIVER})


INFRA = {"label": "Open Infrastructure", "href": "/setup/"}
# (step, words in the error) -> what to do. The first match wins; the step's own default after that.
FAILURES = [
    (("verify", "prove"), "missing environment variables",
     "The app has no JFrog settings (an infrastructure redeploy replaces them). Deliver: Onboard wires them again; "
     "then Verify.", "deliver"),
    (None, "not found", "The workload does not exist yet: deploy it on the Infrastructure page, then run this again.",
     "infra"),
    (None, "sign-in", "Sign in on Settings, then run this again.", None),
    (None, "no azure devops sign-in", "Sign in to Azure on Settings, then run this again.", None),
]
DEFAULTS = {
    "preflight": "Fix the failed checks listed above, then run this again.",
    "discover": "Check the target fields in the configuration, then run this again.",
    "simulate": "Review the failing periods, then run this again.",
    "publish": "Fix the cause above, then Deliver again.",
    "plan": "Fix the cause above, then run the pipeline again.",
    "apply": "Fix the cause above, then run the pipeline again: the plan is made fresh.",
    "wire": "Fix the cause above, then run the pipeline again.",
    "verify": "Deliver again (Onboard re-applies and re-wires the app), then Verify.",
    "prove": "Verify first: when it passes, Prove again.",
    "report": "Run this again.",
}


def after_failure(step: str, error: str, delivery: bool) -> dict:
    """What to do after `step` failed with `error`: {text, action?, link?}. Pure."""
    low = str(error or "").lower()
    for steps, words, text, act in FAILURES:
        if (steps is None or step in steps) and words in low:
            break
    else:
        text, act = DEFAULTS.get(step, "Run this again."), ("deliver" if step in ("verify", "publish") else None)
    out = {"text": text}
    if act == "deliver" and delivery:
        out["action"] = DELIVER
    if act == "infra":
        out["link"] = INFRA
    return out


class Pipelines:
    """Read-only view of the project's pipeline runs (shell http, the az token for ADO)."""

    def __init__(self, sh, organization: str, project: str):
        self.sh = sh
        self.web = f"https://dev.azure.com/{quote(organization)}/{quote(project)}"
        self.base = f"{self.web}/_apis"
        self._token = None
        self._defs = None

    def _get(self, path: str) -> dict:
        if self._token is None:
            res = self.sh.run(["az", "account", "get-access-token", "--resource", ADO_RESOURCE,
                               "--query", "accessToken", "-o", "tsv"], sensitive=True)
            if not res.ok or not res.stdout.strip():
                raise OrderError("cannot check the step order: no Azure DevOps sign-in (sign in to Azure on Settings)")
            self._token = res.stdout.strip()
        sep = "&" if "?" in path else "?"
        res = self.sh.http("GET", f"{self.base}{path}{sep}{API}", headers={"Authorization": f"Bearer {self._token}"},
                           timeout=60)
        if not res.ok:
            raise OrderError(f"cannot check the step order: Azure DevOps answered {res.status or res.error}")
        return res.json({}) or {}

    def _yaml(self) -> dict:
        if self._defs is None:
            self._defs = {d["id"]: str((d.get("process") or {}).get("yamlFilename") or "")
                          for d in self._get("/build/definitions?includeAllProperties=true").get("value") or []}
        return self._defs

    def _run(self, b: dict) -> dict:
        y = self._yaml().get((b.get("definition") or {}).get("id"), "")
        return {"id": b.get("id"), "name": (b.get("definition") or {}).get("name"), "yaml": y,
                "cloud": cloud_of(y), "step": step_of(y), "state": {"notStarted": "queued"}.get(b.get("status"), b.get("status")),
                "result": b.get("result"), "url": f"{self.web}/_build/results?buildId={b.get('id')}"}

    def active(self) -> list:
        builds = self._get("/build/builds?statusFilter=notStarted,inProgress,cancelling&$top=100").get("value") or []
        return [self._run(b) for b in builds]

    def last_onboard(self, cloud: str) -> dict | None:
        want = f"{cloud}-workload-onboard.yml"
        ids = [i for i, y in self._yaml().items() if y.lstrip("/").split("/")[-1] == want]
        if not ids:
            return None
        builds = self._get(f"/build/builds?definitions={ids[0]}&statusFilter=completed&$top=1").get("value") or []
        return self._run(builds[0]) if builds else None
