"""What exists in the logged-in accounts: defaults for a new configuration and the
workloads it can target. Read-only (list/show/config get) through the console's Shell.

    run("gcp", sh)   -> {"defaults": {...}, "workloads": [{kind, name, region, ...}], "errors": [...]}
    run("azure", sh) -> same shape; workloads carry subscription and resource_group.
"""

from __future__ import annotations

import re
from pathlib import Path

from .shell import Shell

DEFAULT_REGION = "us-central1"
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


def _value(sh: Shell, argv) -> str:
    res = sh.run(argv, timeout=30)
    return res.stdout.strip() if res.ok else ""


def gcp(sh: Shell, errors: list) -> tuple[dict, list]:
    project = _value(sh, ["gcloud", "config", "get-value", "project"])
    region = _value(sh, ["gcloud", "config", "get-value", "run/region"]) or DEFAULT_REGION
    out = []
    if not project:
        errors.append("gcloud has no project set (gcloud config set project <id>)")
        return {"project": "", "region": region}, out
    for what in ("services", "jobs"):
        res = sh.run(["gcloud", "run", what, "list", "--project", project, "--format=json"], timeout=90)
        if not res.ok:
            errors.append(f"gcloud run {what} list: {res.error_text(200)}")
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


def azure(sh: Shell, errors: list) -> tuple[dict, list]:
    res = sh.run(["az", "account", "show", "-o", "json"], timeout=30)
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
            errors.append(f"{' '.join(argv[1:])} list: {res.error_text(200)}")
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


def run(cloud: str, sh: Shell, repo_root=None) -> dict:
    errors: list = []
    defaults, workloads = (gcp if cloud == "gcp" else azure)(sh, errors)
    defaults["jfrog_url"] = _jfrog_url(repo_root)
    workloads.sort(key=lambda w: (not w["supported"], w["kind"], w["name"]))
    return {"cloud": cloud, "defaults": defaults, "workloads": workloads, "errors": errors}
