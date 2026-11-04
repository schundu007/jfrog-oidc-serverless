"""Where the running workload's code came from: the image (or package) the cloud runs, the Azure DevOps
build-deploy run that built it, and what that run pulled from JFrog with its OIDC token.

Read-only and best effort: Verify shows it next to the runtime proof and never fails because of it.
The image tag is the commit (12 characters) the build-deploy scripts tag with, which ties the image to its
ADO run. Base images and Python packages come only through JFrog (pipelines/vars/<cloud>-<env>-jfrog.yml names
the remotes); JFrog's own "last downloaded by" on the base image names the pipeline's OIDC user.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from .. import gcprun, model
from . import verify

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
API = "api-version=7.1"
STACK = {"gcp_run_job": "gcp/gcp-cloud-run", "gcp_run_service": "gcp/gcp-cloud-run-service",
         "gcp_run_function": "gcp/gcp-cloud-run-function", "gcp_run_worker_pool": "gcp/gcp-cloud-run-worker-pool",
         "azure_container_app_job": "azure/azure-container-apps", "azure_container_app": "azure/azure-container-app",
         "azure_functions_aca": "azure/azure-functions-aca", "azure_functions_premium": "azure/azure-functions-premium",
         "azure_functions_flex": "azure/azure-functions-flex", "azure_web_app": "azure/azure-web-app"}
BASE_IMAGE = "library/python:3.12-slim"   # pipelines/templates/<cloud>-build-deploy.yml
CODE_KINDS = {"azure_functions_flex", "azure_functions_premium", "azure_web_app", "gcp_run_function"}   # deployed from source
COMMIT_TAG = re.compile(r":([0-9a-f]{12})$")


def pipeline_yaml(kind: str) -> str | None:
    s = STACK.get(kind)
    return f"pipelines/serverless/{s}/build-deploy.yml" if s else None


def image_argv(cfg):
    kind = model.get_path(cfg, "target.kind")
    if kind.startswith("gcp_"):
        n, r, p = verify._g(cfg)
        return [*gcprun.group(gcprun.RESOURCE.get(kind, "services")), "describe", n, "--region", r, "--project", p,
                "--format=json"]
    n, rg, s = verify._a(cfg)
    loc = ["-g", rg, "-n", n, "--subscription", s, "-o", "json", "--only-show-errors"]
    if kind == "azure_container_app_job":
        return ["az", "containerapp", "job", "show", *loc]
    if kind in ("azure_container_app", "azure_functions_aca"):
        return ["az", "containerapp", "show", *loc]
    return ["az", model.az_site(kind), "config", "show", *loc]


def running_image(cfg, sh) -> str:
    kind = model.get_path(cfg, "target.kind")
    r = sh.run(image_argv(cfg), timeout=120)
    data = r.json(None) if r.ok else None
    if not isinstance(data, dict):
        return ""
    if kind.startswith("gcp_"):
        cs = gcprun.pod_spec(data, gcprun.RESOURCE.get(kind, "services")).get("containers") or []
        return (cs[0].get("image") or "") if cs else ""
    if kind in model.SITE_KINDS:
        fx = str(data.get("linuxFxVersion") or "")
        return fx.split("|", 1)[1] if fx.upper().startswith("DOCKER|") else ""
    cs = ((data.get("properties") or {}).get("template") or {}).get("containers") or []
    return (cs[0].get("image") or "") if cs else ""


def _jfrog_remotes(repo_root, cloud: str, env: str) -> dict:
    out = {}
    # The JFrog repository names live in <cloud>-<env>-jfrog.yml (read only by build-deploy and onboarding runs).
    for name in (f"{cloud}-{env}-jfrog.yml", f"{cloud}-{env}.yml"):
        try:
            for line in (Path(repo_root) / "pipelines" / "vars" / name).read_text().splitlines():
                m = re.match(r"\s*(JFROG_DOCKER_REMOTE|JFROG_PYPI_REPO):\s*(\S+)", line)
                if m:
                    out.setdefault(m.group(1), m.group(2))
        except OSError:
            pass
    return out


class AdoReader:
    """Read-only Azure DevOps REST with the operator's az sign-in (the token stays in this object)."""

    def __init__(self, sh, organization: str, project: str):
        self.sh, self.org, self.project = sh, organization, project
        self.base = f"https://dev.azure.com/{quote(organization)}/{quote(project)}/_apis"
        self.web = f"https://dev.azure.com/{quote(organization)}/{quote(project)}"
        self._tok = ""

    def get(self, path: str):
        if not self._tok:
            r = self.sh.run(["az", "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken",
                             "-o", "tsv"], timeout=60, sensitive=True)
            self._tok = r.stdout.strip() if r.ok else ""
            if not self._tok:
                return None
        sep = "&" if "?" in path else "?"
        res = self.sh.http("GET", f"{self.base}{path}{sep}{API}", headers={"Authorization": f"Bearer {self._tok}"}, timeout=60)
        return res.json(None) if res.ok else None

    def build_run(self, yaml_path: str, commit: str = ""):
        """The newest succeeded run of the pipeline running yaml_path, preferring the one built from commit."""
        defs = (self.get("/build/definitions?includeAllProperties=true") or {}).get("value") or []
        d = next((d for d in defs if str((d.get("process") or {}).get("yamlFilename", "")).lstrip("/") == yaml_path), None)
        if not d:
            return None
        runs = (self.get(f"/build/builds?definitions={int(d['id'])}&resultFilter=succeeded&$top=25") or {}).get("value") or []
        pick = next((b for b in runs if commit and str(b.get("sourceVersion", "")).startswith(commit)), None) or \
            (runs[0] if runs else None)
        if not pick:
            return {"pipeline": d.get("name"), "pipeline_url": f"{self.web}/_build?definitionId={int(d['id'])}"}
        return {"pipeline": d.get("name"), "run": pick.get("buildNumber") or pick.get("id"), "id": pick.get("id"),
                "url": f"{self.web}/_build/results?buildId={int(pick['id'])}", "finished": pick.get("finishTime") or "",
                "commit": str(pick.get("sourceVersion") or "")[:12], "matches_image": bool(commit) and
                str(pick.get("sourceVersion", "")).startswith(commit), "by": (pick.get("requestedFor") or {}).get("displayName") or ""}


def run(cfg, sh, delivery: dict | None, repo_root, jfrog=None, env: str = "test") -> dict:
    """{image, commit, from_jfrog, build{...}, jfrog_record{...}}; never raises (reason on a gap)."""
    kind = model.get_path(cfg, "target.kind")
    cloud = model.cloud_of(kind)
    out: dict = {"kind": "source package" if kind in CODE_KINDS else "image"}
    try:
        image = running_image(cfg, sh)
    except Exception:   # noqa: BLE001 - best effort
        image = ""
    out["image"] = image
    m = COMMIT_TAG.search(image or "")
    commit = m.group(1) if m else ""
    out["commit"] = commit
    remotes = _jfrog_remotes(repo_root, cloud, env)
    jhost = ""
    jurl = model.get_path(cfg, "jfrog.url") if isinstance(cfg.get("jfrog"), dict) else ""
    if jurl:
        jhost = str(jurl).split("//", 1)[-1].strip("/")
    from_jfrog = []
    if remotes.get("JFROG_DOCKER_REMOTE") and out["kind"] == "image":
        from_jfrog.append(f"{jhost + '/' if jhost else ''}{remotes['JFROG_DOCKER_REMOTE']}/{BASE_IMAGE}")
    if remotes.get("JFROG_PYPI_REPO") and kind in CODE_KINDS:
        from_jfrog.append(f"Python packages from {remotes['JFROG_PYPI_REPO']}")
    out["from_jfrog"] = from_jfrog
    yaml_path = pipeline_yaml(kind)
    ado = (delivery or {}).get("ado") or {}
    if yaml_path and ado.get("organization") and ado.get("project"):
        try:
            out["build"] = AdoReader(sh, ado["organization"], ado["project"]).build_run(yaml_path, commit)
        except Exception:   # noqa: BLE001
            out["build"] = None
    if jfrog is not None and remotes.get("JFROG_DOCKER_REMOTE") and out["kind"] == "image":
        name, tag = BASE_IMAGE.rsplit(":", 1)
        for leaf in ("list.manifest.json", "manifest.json"):
            try:
                st, data = jfrog.call("GET", f"/artifactory/api/storage/{remotes['JFROG_DOCKER_REMOTE']}-cache/{name}/{tag}/{leaf}?stats")
            except Exception:   # noqa: BLE001
                break
            if st == 200 and isinstance(data, dict):
                out["jfrog_record"] = {"artifact": f"{remotes['JFROG_DOCKER_REMOTE']}/{BASE_IMAGE}",
                                       "last_downloaded_by": data.get("lastDownloadedBy") or "",
                                       "last_downloaded": data.get("lastDownloaded")}
                break
    return out
