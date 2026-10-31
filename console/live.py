"""What the cloud says about each workload right now (read-only): does it still exist, and is it
wired (its first container or app settings carry JFROG_OIDC_PROVIDER)?

One list call per resource type and project/subscription, not one per workload. Only variable
*names* are looked at: app settings are read as a sensitive call and their values dropped.
States: wired, not_wired, missing, unknown (the read failed; detail says why).
"""

from __future__ import annotations

from . import model
from .shell import Shell

WIRED_KEY = "JFROG_OIDC_PROVIDER"


def _names(env) -> set:
    return {e.get("name") for e in env or [] if isinstance(e, dict)}


def _state(found: bool, wired: bool) -> dict:
    if not found:
        return {"state": "missing"}
    return {"state": "wired" if wired else "not_wired"}


# -- GCP ---------------------------------------------------------------------------------------

def _gcp_index(sh: Shell, project: str) -> dict:
    """(collection, region, name) -> env names, for every Cloud Run job and service in the project."""
    out = {}
    for what in ("jobs", "services"):
        res = sh.run(["gcloud", "run", what, "list", "--project", project, "--format=json"], timeout=90)
        if not res.ok:
            raise RuntimeError(f"gcloud run {what} list: {res.error_text(160)}")
        for item in res.json() or []:
            md = item.get("metadata") or {}
            region = (md.get("labels") or {}).get("cloud.googleapis.com/location", "")
            spec = (item.get("spec") or {}).get("template", {}).get("spec", {})
            if what == "jobs":
                spec = spec.get("template", {}).get("spec", {})
            containers = spec.get("containers") or [{}]
            out[(what, region, md.get("name"))] = _names(containers[0].get("env"))
    return out


def gcp(cfgs: list, sh: Shell) -> dict:
    result, index = {}, {}
    for cfg in cfgs:
        g = model.get_path(cfg, "target.gcp") or {}
        kind = model.get_path(cfg, "target.kind")
        project, region = g.get("project"), g.get("region")
        if not (project and region and g.get("name")):
            result[cfg["name"]] = {"state": "unknown", "detail": "no project, region or name"}
            continue
        if project not in index:
            try:
                index[project] = _gcp_index(sh, project)
            except RuntimeError as exc:
                index[project] = exc
        idx = index[project]
        if isinstance(idx, Exception):
            result[cfg["name"]] = {"state": "unknown", "detail": str(idx)}
            continue
        name = (model.get_path(cfg, "target.discovered.run_service") or g["name"]) if kind == "gcp_run_function" else g["name"]
        env = idx.get(("jobs" if kind == "gcp_run_job" else "services", region, name))
        result[cfg["name"]] = _state(env is not None, WIRED_KEY in (env or set()))
    return result


# -- Azure ---------------------------------------------------------------------------------------

CONTAINER_LISTS = {"azure_container_app": ["az", "containerapp", "list"],
                   "azure_functions_aca": ["az", "containerapp", "list"],
                   "azure_container_app_job": ["az", "containerapp", "job", "list"]}


def azure(cfgs: list, sh: Shell) -> dict:
    result, lists = {}, {}

    def listing(argv, sub):
        key = (tuple(argv), sub)
        if key not in lists:
            res = sh.run([*argv, "--subscription", sub, "-o", "json"], timeout=120)
            lists[key] = ({(d.get("resourceGroup", "").lower(), d.get("name")): d for d in res.json() or []}
                          if res.ok else RuntimeError(f"{' '.join(argv)}: {res.error_text(160)}"))
        return lists[key]

    for cfg in cfgs:
        a = model.get_path(cfg, "target.azure") or {}
        kind, sub, rg, name = model.get_path(cfg, "target.kind"), a.get("subscription"), a.get("resource_group"), a.get("name")
        if not (sub and rg and name):
            result[cfg["name"]] = {"state": "unknown", "detail": "no subscription, resource group or name"}
            continue
        rid = (model.get_path(cfg, "target.discovered.resource_id") or "").lower()
        web = kind in ("azure_functions_premium", "azure_functions_flex") or "/microsoft.web/" in rid
        found = listing(["az", "functionapp", "list"] if web else CONTAINER_LISTS[kind], sub)
        if isinstance(found, Exception):
            result[cfg["name"]] = {"state": "unknown", "detail": str(found)}
            continue
        app = found.get((rg.lower(), name))
        if app is None:
            result[cfg["name"]] = {"state": "missing"}
        elif web:
            res = sh.run(["az", "functionapp", "config", "appsettings", "list", "-g", rg, "-n", name,
                          "--subscription", sub, "-o", "json"], timeout=120, sensitive=True)
            result[cfg["name"]] = (_state(True, WIRED_KEY in _names(res.json())) if res.ok
                                   else {"state": "unknown", "detail": f"app settings: {res.error_text(160)}"})
        else:
            containers = ((app.get("properties") or {}).get("template") or {}).get("containers") or [{}]
            result[cfg["name"]] = _state(True, WIRED_KEY in _names(containers[0].get("env")))
    return result


def status(cloud: str, cfgs: list, sh: Shell) -> dict:
    """{config name: {state, detail?}} for one cloud's configs. Other clouds: unknown."""
    if cloud == "gcp":
        return gcp(cfgs, sh)
    if cloud == "azure":
        return azure(cfgs, sh)
    return {c["name"]: {"state": "unknown", "detail": "live status is read for Google Cloud and Azure"} for c in cfgs}
