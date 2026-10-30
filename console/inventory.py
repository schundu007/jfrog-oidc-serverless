"""What exists in the logged-in accounts: defaults for a new configuration and the
workloads it can target, whether the console created them or not. Read-only
(list/show/config get) through the console's Shell.

    run("gcp", sh, scope=<project>)        -> {"defaults": {...}, "scopes": [{id, name}],
                                               "workloads": [{kind, name, region, onboarded, ...}], "errors": [...]}
    run("azure", sh, scope=<subscription>) -> same shape; workloads carry subscription and resource_group.

scopes are every project / subscription the login can see; the workloads are those
of one scope (the CLI default unless one is picked). onboarded marks a workload a
saved config or a delivered workspace (terraform/workspaces/*/configs) already targets.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path

from .shell import Shell

DEFAULT_REGION = "us-central1"
SCOPE_RE = {"gcp": re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
            "azure": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)}
LOCATION = "cloud.googleapis.com/location"


def _jfrog_url(repo_root) -> str:
    """JFROG_URL from pipelines/vars/test.yml, the platform the pipelines already use."""
    if repo_root is None:
        return ""
    p = Path(repo_root) / "pipelines" / "vars" / "test.yml"
    try:
        m = re.search(r"^\s*JFROG_URL:\s*(\S+)\s*$", p.read_text(), re.M)
    except OSError:
        return ""
    return m.group(1).strip("'\"").rstrip("/") if m else ""


API_DISABLED = re.compile(r"SERVICE_DISABLED|has not been used in project|API has not been used", re.I)
AUTH_FAILED = re.compile(r"AADSTS\d+|az login|Reauthentication|refresh token|invalid_grant", re.I)


def _first_error(res, limit: int = 200) -> str:
    """The CLI's own ERROR line (az/gcloud print hints after it), not the tail."""
    lines = [ln.strip() for ln in res.error_text(2000).splitlines() if ln.strip()]
    line = next((ln for ln in lines if ln.startswith("ERROR")), lines[0] if lines else "failed")
    return line[:limit]


def _value(sh: Shell, argv) -> str:
    res = sh.run(argv, timeout=30)
    return res.stdout.strip() if res.ok else ""


def _gcp_scopes(sh: Shell, errors: list) -> list:
    res = sh.run(["gcloud", "projects", "list", "--format=json"], timeout=90)
    if not res.ok:
        errors.append(f"gcloud projects list: {res.error_text(200)}")
        return []
    return sorted(({"id": p.get("projectId", ""), "name": p.get("name") or p.get("projectId", "")}
                   for p in res.json() or [] if p.get("projectId") and p.get("lifecycleState", "ACTIVE") == "ACTIVE"),
                  key=lambda p: p["id"])


def gcp(sh: Shell, errors: list, scope: str | None = None) -> tuple[dict, list]:
    project = scope or _value(sh, ["gcloud", "config", "get-value", "project"])
    region = _value(sh, ["gcloud", "config", "get-value", "run/region"]) or DEFAULT_REGION
    out = []
    if not project:
        errors.append("gcloud has no project set (gcloud config set project <id>)")
        return {"project": "", "region": region}, out
    for what in ("services", "jobs"):
        res = sh.run(["gcloud", "run", what, "list", "--project", project, "--format=json"], timeout=90)
        if not res.ok:
            if API_DISABLED.search(res.error_text(2000)):
                errors.append(f"Cloud Run is not enabled in {project}: nothing to onboard there")
                break
            errors.append(f"gcloud run {what} list: {_first_error(res)}")
            continue
        for item in res.json() or []:
            md = item.get("metadata") or {}
            labels = md.get("labels") or {}
            if what == "jobs":
                kind = "gcp_run_job"
            elif labels.get("goog-managed-by") == "cloudfunctions":
                kind = "gcp_run_function"
            else:
                kind = "gcp_run_service"
            out.append({"kind": kind, "name": md.get("name", ""), "project": project,
                        "region": labels.get(LOCATION, region), "supported": True})
    return {"project": project, "region": region}, out


def _azure_scopes(sh: Shell, errors: list) -> list:
    res = sh.run(["az", "account", "list", "-o", "json"], timeout=60)
    if not res.ok:
        errors.append(f"az account list: {res.error_text(200)}")
        return []
    return sorted(({"id": a.get("id", ""), "name": a.get("name") or a.get("id", "")}
                   for a in res.json() or [] if a.get("id") and a.get("state", "Enabled") == "Enabled"
                   # A tenant with no subscriptions lists as one whose id is the tenant id.
                   and a.get("id") != a.get("tenantId")),
                  key=lambda a: a["name"].lower())


def azure(sh: Shell, errors: list, scope: str | None = None) -> tuple[dict, list]:
    res = sh.run(["az", "account", "show", *(["--subscription", scope] if scope else []), "-o", "json"], timeout=30)
    acct = (res.json() or {}) if res.ok else {}
    sub = acct.get("id", "")
    out = []
    if not sub:
        errors.append("az is not logged in (az login)")
        return {"subscription": ""}, out
    lists = (("containerapp", ["az", "containerapp", "list"]),
             ("job", ["az", "containerapp", "job", "list"]),
             ("functionapp", ["az", "functionapp", "list"]))
    for what, argv in lists:
        res = sh.run([*argv, "--subscription", sub, "-o", "json"], timeout=120)
        if not res.ok:
            if AUTH_FAILED.search(res.error_text(2000)):
                # Same login for every list: one clear line, not three.
                errors.append(f"the az login cannot read subscription {acct.get('name') or sub}: {_first_error(res, 120)} "
                              f"(az login --tenant {acct.get('tenantId') or '<tenant>'})")
                break
            errors.append(f"az {' '.join(argv[1:])}: {_first_error(res)}")
            continue
        for d in res.json() or []:
            supported = True
            if what == "job":
                kind = "azure_container_app_job"
            elif what == "containerapp":
                kind = "azure_functions_aca" if "functionapp" in str(d.get("kind") or "").lower() else "azure_container_app"
            else:
                kind = "azure_functions_flex" if "flex" in str(d.get("sku") or "").lower() else "azure_functions_premium"
            out.append({"kind": kind, "name": d.get("name", ""), "subscription": sub,
                        "resource_group": d.get("resourceGroup", ""), "region": d.get("location", ""),
                        "supported": supported})
    return {"subscription": sub, "subscription_name": acct.get("name", "")}, out


def _key(cloud: str, t: dict) -> tuple:
    if cloud == "gcp":
        return (t.get("project"), t.get("region"), t.get("name"))
    return (str(t.get("subscription") or "").lower(), str(t.get("resource_group") or "").lower(), t.get("name"))


def onboarded_keys(cloud: str, configs) -> set:
    """Targets the given configs (dicts) already cover."""
    keys = set()
    for cfg in configs:
        kind = (cfg.get("target") or {}).get("kind") or ""
        if kind.startswith("azure") == (cloud == "azure"):
            keys.add(_key(cloud, (cfg.get("target") or {}).get(cloud) or {}))
    return keys


def delivered_configs(repo_root) -> list:
    """Configs committed with delivered workspaces (terraform/workspaces/<ws>/configs/*.json)."""
    out = []
    if repo_root is None:
        return out
    for p in sorted((Path(repo_root) / "terraform" / "workspaces").glob("*/configs/*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


def fetch(cloud: str, sh: Shell, scope: str | None = None) -> dict:
    """The live listing: the slow part (minutes across many projects), cacheable."""
    errors: list = []
    if scope and not SCOPE_RE[cloud].match(scope):
        return {"cloud": cloud, "defaults": {}, "scopes": [], "workloads": [],
                "errors": [f"not a {'project ID' if cloud == 'gcp' else 'subscription ID'}: {scope!r}"]}
    scopes = (_gcp_scopes if cloud == "gcp" else _azure_scopes)(sh, errors)
    defaults, workloads = (gcp if cloud == "gcp" else azure)(sh, errors, scope)
    return {"cloud": cloud, "defaults": defaults, "scopes": scopes, "workloads": workloads, "errors": errors,
            "fetched_at": int(time.time())}


def annotate(cloud: str, raw: dict, repo_root=None, configs=()) -> dict:
    """A fetched listing plus what is local and changes often: the JFrog URL and onboarded marks."""
    out = copy.deepcopy(raw)
    out["defaults"]["jfrog_url"] = _jfrog_url(repo_root)
    done = onboarded_keys(cloud, [*configs, *delivered_configs(repo_root)])
    for w in out["workloads"]:
        w["onboarded"] = _key(cloud, w) in done
    out["workloads"].sort(key=lambda w: (not w["supported"], w["onboarded"], w["kind"], w["name"]))
    return out


def run(cloud: str, sh: Shell, repo_root=None, scope: str | None = None, configs=()) -> dict:
    return annotate(cloud, fetch(cloud, sh, scope), repo_root, configs)


class Cache:
    """Fetched listings by cloud and scope, kept in .console/inventory.json (IDs and names, no secrets)
    so a page load or a restart doesn't wait on the clouds. Refresh replaces an entry."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        try:
            self._data = json.loads(self.path.read_text()) if self.path else {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, cloud: str, scope: str | None) -> dict | None:
        with self._lock:
            hit = self._data.get(f"{cloud}:{scope or ''}")
            return copy.deepcopy(hit) if hit else None

    def put(self, cloud: str, scope: str | None, raw: dict) -> None:
        if raw.get("errors"):
            return
        resolved = (raw.get("defaults") or {}).get("project" if cloud == "gcp" else "subscription") or ""
        with self._lock:
            for s in {scope or "", resolved}:
                self._data[f"{cloud}:{s}"] = copy.deepcopy(raw)
            if not self.path:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data))
                tmp.replace(self.path)
            except OSError:
                pass
