"""Console config: defaults per target kind, validation, cron and TTL math.

A config is a plain JSON object (spec §3). It holds no secrets and is safe to
commit. validate() is the single source of the rules the UI shows inline
(served through /api/validate) and the simulator relies on.
"""

from __future__ import annotations

import copy
import re

KINDS = {
    "gcp_run_service": {"cloud": "gcp", "label": "Cloud Run service"},
    "gcp_run_job": {"cloud": "gcp", "label": "Cloud Run job"},
    "gcp_run_function": {"cloud": "gcp", "label": "Cloud Run function"},
    "gcp_run_worker_pool": {"cloud": "gcp", "label": "Cloud Run worker pool"},
    "gcp_app_engine": {"cloud": "gcp", "label": "App Engine service"},
    "azure_container_app": {"cloud": "azure", "label": "Container App"},
    "azure_container_app_job": {"cloud": "azure", "label": "Container Apps job"},
    "azure_functions_aca": {"cloud": "azure", "label": "Functions on Container Apps"},
    "azure_functions_premium": {"cloud": "azure", "label": "Functions Premium/Dedicated"},
    "azure_functions_flex": {"cloud": "azure", "label": "Functions Flex Consumption"},
    "azure_web_app": {"cloud": "azure", "label": "Web App"},
}

# App Service sites (Microsoft.Web/sites): wired through app settings, reached on their default host name.
SITE_KINDS = ("azure_functions_premium", "azure_functions_flex", "azure_web_app")


def az_site(kind: str) -> str:
    """The az command group for an App Service site: a web app or a function app."""
    return "webapp" if kind == "azure_web_app" else "functionapp"


# Where each cloud's target lives in the config, and the fields that identify it.
TARGET_FIELDS = {
    "gcp": ("project", "region", "name"),
    "azure": ("subscription", "resource_group", "name"),
}
EMPTY_TARGET = {
    "gcp": {"project": "", "region": "", "name": ""},
    "azure": {"subscription": "", "resource_group": "", "name": "", "identity_type": "user_assigned"},
}
SAMPLE_TARGET = {
    "gcp": {"project": "my-project", "region": "us-central1"},
    "azure": {"subscription": "00000000-0000-0000-0000-000000000000", "resource_group": "rg-apps"},
}
# Runtime path only on every cloud: images reach the platform's own registry at build time
# (the ADO pipeline's OIDC token to JFrog), and the platform pulls with its identity.
# Default integration names.
DEFAULT_INTEGRATIONS = {"gcp": "gcp-cloudrun", "azure": "azure-entra"}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

# Runtime tokens are refreshed by the client 5 min before expiry, so a lifetime
# past 1 h only widens the window a leaked token stays valid. Same cap as the
# AKS pilot's mapping rules (docs/aks-pilot-lessons.md).
RUNTIME_TTL_MAX = 3600


def jfrog_origin_error(url: str):
    """None if url is a bare https origin (https://host), else why it is not.

    The client appends /access/api/v1/oidc/token and /artifactory/... itself, so
    a path here doubles up. On AKS the same class of mistake (a scheme the
    plugin also prepends) produced https://https://... and failed only in the
    plugin's own log.
    """
    from urllib.parse import urlsplit
    url = str(url or "")
    if not url.startswith("https://"):
        return "must start with https://"
    parts = urlsplit(url.rstrip("/"))
    if parts.path or parts.query or parts.fragment or parts.username or parts.password or parts.port:
        return "must be the platform origin only (https://host), with no path such as /artifactory, query, port or credentials"
    if not HOST_RE.match(parts.hostname or ""):
        return "has no valid host name"
    return None


def jfrog_host(cfg: dict) -> str:
    from urllib.parse import urlsplit
    return urlsplit(str(get_path(cfg, "jfrog.url", "")).rstrip("/")).hostname or ""


def docker_host(cfg: dict) -> str:
    """The Docker registry host workloads pull from: the repo's dedicated host.

    JFrog SaaS serves each Docker repo at <server>-<repo-key>.jfrog.io (the
    subdomain method). The repository-path method (<server>.jfrog.io/<repo-key>/...)
    failed on the AKS pilot's instance for local repos ("Unsupported v2 repository
    request for ''", token realm without the repo key), so every pull here goes
    to the dedicated host. jfrog.docker_host overrides the derived name, e.g. for
    a self-hosted platform with its own reverse-proxy naming.
    """
    explicit = str(get_path(cfg, "jfrog.docker_host", "") or "").strip().lower()
    if explicit:
        return explicit
    host = jfrog_host(cfg)
    repo = str(get_path(cfg, "jfrog.docker_repo", "") or "").strip().lower()
    if not host or not repo or "." not in host:
        return ""
    server, rest = host.split(".", 1)
    return f"{server}-{repo}.{rest}"


def dedicated_identity_name(cfg: dict) -> str:
    """The identity the workspace creates for an existing app that has none of its own.

    GCP: a service account ID (6-30 chars, starts with a letter): jfrog-<name>.
    Azure: a user-assigned identity name (CAF prefix): id-<name>-jfrog.
    Discover also uses it to recognise an identity the console created earlier.
    """
    name = str(cfg.get("name") or "")
    cloud = cloud_of(get_path(cfg, "target.kind"))
    if cloud == "gcp":
        return f"jfrog-{name[:24]}".rstrip("-")
    return f"id-{name}-jfrog"


def cloud_of(kind: str) -> str:
    return KINDS[kind]["cloud"]


# Token audience on Azure: an Entra app registration (api://<app-id>), or Microsoft's
# api://AzureADTokenExchange with nothing created in Entra.
AZURE_AUDIENCES = ("app_registration", "token_exchange")


def default_config(kind: str, name: str = "new-target") -> dict:
    cloud = cloud_of(kind)
    short = kind.split("_", 1)[1].replace("_", "-")
    return {
        "name": name,
        "jfrog": {
            "url": "https://acme.jfrog.io",
            # Docker pulls go to the repo's dedicated host (see docker_host()).
            "docker_repo": "docker-virtual",
            "docker_host": "",            # override; empty = <server>-<docker_repo>.<domain>
            "reader_group": "serverless-readers",
            "project_key": None,
            "integration_names": dict(DEFAULT_INTEGRATIONS),
            # Set by preflight when the integration already exists for the same issuer.
            "adopt_integration": False,
            **({"azure_audience": "app_registration"} if cloud == "azure" else {}),
        },
        "target": {
            "kind": kind,
            **copy.deepcopy(EMPTY_TARGET),
            cloud: {**EMPTY_TARGET[cloud], **SAMPLE_TARGET[cloud], "name": name},
        },
        "runtime": {"enabled": True, "ttl_seconds": 3600, "username": f"svc-{cloud}-{short}"},
        "verify": {
            # Azure reads the file its JFrog trust stack seeds (azure-jfrog-trust repositories.tf).
            "artifact_path": "azure-generic-local/hello.txt" if cloud == "azure" else "generic-local/healthcheck/ping.txt",
            "probe_image_runtime": "",
        },
        "state_backend": {"type": "local"},
    }


def get_path(cfg: dict, dotted: str, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def merge(base: dict, over: dict) -> dict:
    """Deep-merge over onto a copy of base."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ----------------------------------------------------------------- validation


def validate(cfg: dict) -> dict:
    errors, warnings = [], []

    def err(path, code, msg):
        errors.append({"path": path, "code": code, "message": msg})

    def warn(path, code, msg):
        warnings.append({"path": path, "code": code, "message": msg})

    if not NAME_RE.match(str(cfg.get("name", ""))):
        err("name", "name", "lowercase letters, digits and dashes, starting with a letter or digit (max 63)")
    url_problem = jfrog_origin_error(get_path(cfg, "jfrog.url", ""))
    if url_problem:
        code = "jfrog_url_https" if url_problem.startswith("must start") else "jfrog_url_origin"
        err("jfrog.url", code, f"JFrog URL {url_problem}")
    dh_explicit = str(get_path(cfg, "jfrog.docker_host", "") or "").strip()
    if dh_explicit and not HOST_RE.match(dh_explicit.lower()):
        err("jfrog.docker_host", "docker_host_bare",
            "Docker host must be a bare host name (no https:// and no path), e.g. acme-docker-virtual.jfrog.io")
    elif not url_problem and docker_host(cfg) and docker_host(cfg) == jfrog_host(cfg):
        warn("jfrog.docker_host", "docker_host_is_platform",
             "Docker pulls through the platform host use the repository-path method, which failed for local repos "
             "on the AKS pilot; use the repo's dedicated host (<server>-<repo-key>.jfrog.io)")
    kind = get_path(cfg, "target.kind")
    if kind not in KINDS:
        err("target.kind", "unknown_kind", f"unknown target kind {kind!r}")
        return {"errors": errors, "warnings": warnings}
    cloud = cloud_of(kind)
    for f in TARGET_FIELDS[cloud]:
        if not get_path(cfg, f"target.{cloud}.{f}"):
            err(f"target.{cloud}.{f}", "required", f"{f} is required for {KINDS[kind]['label']}")
    if not get_path(cfg, f"jfrog.integration_names.{cloud}"):
        err(f"jfrog.integration_names.{cloud}", "required", "integration name is required")
    if cloud == "azure" and (get_path(cfg, "jfrog.azure_audience") or "app_registration") not in AZURE_AUDIENCES:
        err("jfrog.azure_audience", "azure_audience", f"one of {', '.join(AZURE_AUDIENCES)}")

    rt = get_path(cfg, "runtime", {}) or {}
    if rt.get("enabled", True):
        ttl = int(rt.get("ttl_seconds") or 0)
        if not 300 <= ttl <= RUNTIME_TTL_MAX:
            err("runtime.ttl_seconds", "runtime_ttl_range",
                "runtime TTL must be between 300 s and 1 h: the client refreshes before expiry, so longer only widens the leak window")
        elif ttl < 900:
            warn("runtime.ttl_seconds", "runtime_ttl_short", "runtime TTL under 15 min means frequent exchanges on every warm instance")
        if not rt.get("username"):
            err("runtime.username", "required", "runtime username is required")

    if cloud == "azure" and get_path(cfg, "target.azure.identity_type") == "system_assigned":
        warn("target.azure.identity_type", "system_assigned_identity",
             "live probes cannot borrow a system-assigned identity; verification will use the /healthz/jfrog self-test endpoint")

    return {"errors": errors, "warnings": warnings}


def schema() -> dict:
    """What the UI needs to render the form: kinds, defaults, and the field list."""
    return {
        "kinds": {k: {"cloud": v["cloud"], "label": v["label"]} for k, v in KINDS.items()},
        "defaults": {k: default_config(k) for k in KINDS},
        "target_fields": {c: list(f) for c, f in TARGET_FIELDS.items()},
    }
