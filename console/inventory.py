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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .shell import Shell

DEFAULT_REGION = "us-central1"
SCOPE_RE = {"gcp": re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
            "azure": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),
            "aws": re.compile(r"^[A-Za-z0-9_.@+-]{1,64}$"),                  # an AWS CLI profile
            "openshift": re.compile(r"^[A-Za-z0-9_.:/@+-]{1,253}$")}         # a kube context
# The defaults field that names the listed scope.
SCOPE_KEY = {"gcp": "project", "azure": "subscription", "aws": "profile", "openshift": "context"}
AWS_DEFAULT_REGION = "us-east-1"
ECS_FAMILY_CAP = 50
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


# Only projects / subscriptions with something to onboard are listed (the picked one always is).
AZURE_WORKLOADS = ("[?type=='Microsoft.App/containerApps' || type=='Microsoft.App/jobs' || "
                   "(type=='Microsoft.Web/sites' && contains(kind, 'functionapp'))] | length(@)")


def _gcp_has_workloads(sh: Shell, project: str) -> bool:
    for what in ("services", "jobs"):
        res = sh.run(["gcloud", "run", what, "list", "--project", project, "--format=json"], timeout=90)
        if res.ok and res.json():
            return True
    return False


def _azure_has_workloads(sh: Shell, sub: str) -> bool:
    res = sh.run(["az", "resource", "list", "--subscription", sub, "--query", AZURE_WORKLOADS, "-o", "json"],
                 timeout=90)
    return res.ok and bool(res.json())


def _with_workloads(cloud: str, sh: Shell, scopes: list, keep: str) -> list:
    has = _gcp_has_workloads if cloud == "gcp" else _azure_has_workloads
    others = [s for s in scopes if s["id"] != keep]
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(zip((s["id"] for s in others), pool.map(lambda s: has(sh, s["id"]), others)))
    return [s for s in scopes if s["id"] == keep or found.get(s["id"])]


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


def _aws_scopes(sh: Shell, errors: list) -> list:
    res = sh.run(["aws", "configure", "list-profiles"], timeout=30)
    if not res.ok:
        errors.append(f"aws configure list-profiles: {res.error_text(200)}")
        return []
    return [{"id": p, "name": p} for p in sorted({ln.strip() for ln in res.stdout.splitlines() if ln.strip()})
            if SCOPE_RE["aws"].match(p)]


def aws(sh: Shell, errors: list, scope: str | None = None) -> tuple[dict, list]:
    """Lambda functions, ECS services (and task families no service runs) and App Runner services
    of one profile's account, in the profile's default region."""
    prof = ["--profile", scope] if scope else []
    # Listing needs a region; only a configured one becomes a default for the form.
    configured = _value(sh, ["aws", "configure", "get", "region", *prof])
    region = configured or AWS_DEFAULT_REGION
    o = ["--region", region, *prof, "--output", "json"]
    res = sh.run(["aws", "sts", "get-caller-identity", *o], timeout=60)
    account = ((res.json() or {}) if res.ok else {}).get("Account", "")
    defaults = {"profile": scope or "", "account": account, "region": configured}
    out: list = []
    if not account:
        errors.append(f"aws is not logged in{f' for profile {scope}' if scope else ''}: {_first_error(res, 160)} "
                      f"(aws sso login{f' --profile {scope}' if scope else ''})")
        return defaults, out
    base = {"account": account, "region": region, "profile": scope or "", "cluster": "", "supported": True}

    def listing(argv, what):
        r = sh.run([*argv, *o], timeout=120)
        if not r.ok:
            errors.append(f"{what}: {_first_error(r)}")
            return None
        return r.json()

    for name in listing(["aws", "lambda", "list-functions", "--query", "Functions[].FunctionName"], "lambda list-functions") or []:
        out.append({**base, "kind": "aws_lambda", "name": name})
    served = set()
    for arn in (listing(["aws", "ecs", "list-clusters"], "ecs list-clusters") or {}).get("clusterArns") or []:
        cluster = arn.rsplit("/", 1)[-1]
        for sarn in (listing(["aws", "ecs", "list-services", "--cluster", cluster], "ecs list-services") or {}).get("serviceArns") or []:
            name = sarn.rsplit("/", 1)[-1]
            served.add(name)
            out.append({**base, "kind": "aws_ecs_task", "name": name, "cluster": cluster})
    fams = (listing(["aws", "ecs", "list-task-definition-families", "--status", "ACTIVE"], "ecs list-task-definition-families")
            or {}).get("families") or []
    for fam in [f for f in fams if f not in served][:ECS_FAMILY_CAP]:
        out.append({**base, "kind": "aws_ecs_task", "name": fam})
    for s in (listing(["aws", "apprunner", "list-services"], "apprunner list-services") or {}).get("ServiceSummaryList") or []:
        out.append({**base, "kind": "aws_app_runner", "name": s.get("ServiceName", "")})
    return defaults, out


# Contexts that point at OpenShift: an `oc login` context (namespace/api-host:6443/user) or an
# OpenShift API server (api.<cluster>:6443, *.openshiftapps.com). AKS, GKE, EKS and local clusters are left out.
OPENSHIFT_SERVER_RE = re.compile(r"^https://api\.[^/:]+:6443/?$|openshiftapps\.com|\.openshift\.")


def _openshift_contexts(sh: Shell, errors: list) -> list:
    res = sh.run(["kubectl", "config", "view", "-o", "json"], timeout=30)
    if not res.ok:
        errors.append(f"kubectl config view: {res.error_text(200)}")
        return []
    cfg = res.json() or {}
    servers = {c.get("name"): (c.get("cluster") or {}).get("server", "") for c in cfg.get("clusters") or []}
    out = []
    for c in cfg.get("contexts") or []:
        name = c.get("name") or ""
        server = servers.get((c.get("context") or {}).get("cluster"), "")
        oc_name = name.count("/") >= 2 and ":6443" in name
        if SCOPE_RE["openshift"].match(name) and (oc_name or OPENSHIFT_SERVER_RE.search(server)):
            out.append(name)
    return sorted(set(out))


def _k8s_scopes(sh: Shell, errors: list) -> list:
    return [{"id": c, "name": c} for c in _openshift_contexts(sh, [])]


def cluster_slug(context: str) -> str:
    """A workspace-safe short name from a kube context (OpenShift contexts look like ns/api-host:6443/user)."""
    host = context.split("/")[1] if context.count("/") >= 2 else context
    host = re.sub(r"^api[-.]", "", host.split(":")[0])
    slug = re.sub(r"[^a-z0-9-]+", "-", host.lower()).strip("-")[:32].strip("-")
    return slug if len(slug) >= 2 and slug[0].isalpha() else f"k8s-{slug}"[:32].strip("-")


def openshift(sh: Shell, errors: list, scope: str | None = None) -> tuple[dict, list]:
    """Knative services (ksvc) in every namespace of one kube context."""
    contexts = _openshift_contexts(sh, errors)
    current = _value(sh, ["kubectl", "config", "current-context"])
    ctx = scope or (current if current in contexts else contexts[0] if contexts else "")
    if not ctx or ctx not in contexts:
        errors.append("No OpenShift cluster in kubeconfig: log in with oc login <api-url>"
                      if not ctx else f"{ctx} is not an OpenShift context")
        return {"context": ""}, []
    defaults = {"context": ctx, "cluster": cluster_slug(ctx)}
    res = sh.run(["kubectl", "get", "ksvc", "--all-namespaces", "-o", "json", "--context", ctx], timeout=120)
    if not res.ok:
        text = res.error_text(2000)
        errors.append("Knative Serving is not installed on this cluster (no ksvc resource)"
                      if "the server doesn't have a resource type" in text else f"kubectl get ksvc: {_first_error(res)}")
        return defaults, []
    out = []
    for item in (res.json() or {}).get("items") or []:
        md = item.get("metadata") or {}
        out.append({"kind": "openshift_knative_service", "name": md.get("name", ""), "namespace": md.get("namespace", ""),
                    "context": ctx, "cluster": defaults["cluster"], "region": md.get("namespace", ""),
                    "supported": True})
    return defaults, out


def _key(cloud: str, t: dict) -> tuple:
    if cloud == "gcp":
        return (t.get("project"), t.get("region"), t.get("name"))
    if cloud == "aws":
        return (t.get("account"), t.get("region"), t.get("name"))
    if cloud == "openshift":
        return (t.get("context"), t.get("namespace"), t.get("name"))
    return (str(t.get("subscription") or "").lower(), str(t.get("resource_group") or "").lower(), t.get("name"))


def onboarded_keys(cloud: str, configs) -> set:
    """Targets the given configs (dicts) already cover."""
    from .model import KINDS
    keys = set()
    for cfg in configs:
        kind = (cfg.get("target") or {}).get("kind") or ""
        if KINDS.get(kind, {}).get("cloud") == cloud:
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
        what = {"gcp": "project ID", "azure": "subscription ID", "aws": "AWS CLI profile", "openshift": "kube context"}
        return {"cloud": cloud, "defaults": {}, "scopes": [], "workloads": [],
                "errors": [f"not a {what[cloud]}: {scope!r}"]}
    scopes = {"gcp": _gcp_scopes, "azure": _azure_scopes, "aws": _aws_scopes, "openshift": _k8s_scopes}[cloud](sh, errors)
    defaults, workloads = {"gcp": gcp, "azure": azure, "aws": aws, "openshift": openshift}[cloud](sh, errors, scope)
    if cloud in ("gcp", "azure"):
        # Profiles and contexts each need their own login: listing them all would prompt or fail per scope.
        scopes = _with_workloads(cloud, sh, scopes, defaults.get(SCOPE_KEY[cloud]) or "")
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
    so quick page loads don't wait on the clouds. An entry older than max_age seconds is fetched again,
    so deleted workloads drop out of the picker. Refresh replaces an entry."""

    MAX_AGE = 60

    def __init__(self, path=None, max_age: int = MAX_AGE, clock=time.time):
        self.path = Path(path) if path else None
        self.max_age, self.clock = max_age, clock
        self._lock = threading.Lock()
        try:
            self._data = json.loads(self.path.read_text()) if self.path else {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, cloud: str, scope: str | None) -> dict | None:
        with self._lock:
            hit = self._data.get(f"{cloud}:{scope or ''}")
            if not hit or self.clock() - hit.get("fetched_at", 0) > self.max_age:
                return None
            return copy.deepcopy(hit)

    def put(self, cloud: str, scope: str | None, raw: dict) -> None:
        if raw.get("errors"):
            return
        resolved = (raw.get("defaults") or {}).get(SCOPE_KEY[cloud]) or ""
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
