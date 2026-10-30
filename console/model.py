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
    "azure_container_app": {"cloud": "azure", "label": "Container App"},
    "azure_container_app_job": {"cloud": "azure", "label": "Container Apps job"},
    "azure_functions_aca": {"cloud": "azure", "label": "Functions on Container Apps"},
    "azure_functions_premium": {"cloud": "azure", "label": "Functions Premium/Dedicated"},
    "azure_functions_flex": {"cloud": "azure", "label": "Functions Flex Consumption"},
}

ACA_PICKUP_LAG = 30 * 60          # Container Apps refreshes KV-backed secrets within ~30 min
PREMIUM_NO_REFRESH_LAG = 24 * 3600  # App Service caches KV references for 24 h

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
    if cloud_of(get_path(cfg, "target.kind")) == "gcp":
        return f"jfrog-{name[:24]}".rstrip("-")
    return f"id-{name}-jfrog"


def cloud_of(kind: str) -> str:
    return KINDS[kind]["cloud"]


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
            "integration_names": {"gcp": "gcp-cloudrun", "azure": "azure-entra"},
            # Set by preflight when the integration already exists for the same issuer.
            "adopt_integration": False,
        },
        "target": {
            "kind": kind,
            "gcp": {"project": "my-project", "region": "us-central1", "name": name} if cloud == "gcp"
            else {"project": "", "region": "", "name": ""},
            "azure": {"subscription": "00000000-0000-0000-0000-000000000000", "resource_group": "rg-apps",
                      "name": name, "environment_id": "", "key_vault_id": "", "identity_type": "user_assigned"}
            if cloud == "azure" else {"subscription": "", "resource_group": "", "name": "", "environment_id": "",
                                      "key_vault_id": "", "identity_type": "user_assigned"},
        },
        "runtime": {"enabled": True, "ttl_seconds": 3600, "username": f"svc-{cloud}-{short}"},
        "pull_path": {
            "enabled": kind != "azure_functions_flex",
            "ttl_seconds": 14400,
            "rotation_cron": "23 * * * *",
            "min_ttl_seconds": 1800,   # rotate.py and module default
            "alert_after_seconds": 10800,
            "function_app_refresh": True,
            # An empty DOCKER upstream_uri means https://<docker_host(cfg)>.
            "gcp_repositories": {"jfrog-docker": {"format": "DOCKER", "upstream_uri": ""}}
            if cloud == "gcp" else {},
            "registry_username": f"svc-{cloud}-registry-pull",
            # Rotator image: host it outside JFrog (AR / ACR) so it still starts
            # when the token it refreshes has lapsed.
            "rotator_image": "",
            "rotator_acr_id": None,       # Azure: ACR resource ID; the rotator identity gets AcrPull
            "name_prefix": "jfrog",       # resource names; must differ per region in one GCP project
        },
        "verify": {
            "artifact_path": "generic-local/healthcheck/ping.txt",
            "probe_image_runtime": "",
            # Relative to the Docker host; on GCP to the AR remote repo. Use a tag no
            # node or AR cache has pulled yet, or the pull proves nothing about auth.
            "probe_image_pull": "jfrog-oidc-probe:0.1.0",
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


# ----------------------------------------------------------------------- cron


def _parse_field(field: str, lo: int, hi: int) -> list:
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"bad step in {field!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"bad range in {field!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = hi
        else:
            raise ValueError(f"bad cron field {field!r}")
        if start < lo or end > hi or start > end:
            raise ValueError(f"{field!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return sorted(values)


class Cron:
    """Minute and hour fields of a 5-field UTC cron; day fields must be '*'."""

    def __init__(self, expr: str):
        fields = (expr or "").split()
        if len(fields) != 5:
            raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
        if fields[2:] != ["*", "*", "*"]:
            raise ValueError("only minute and hour fields are supported; day fields must be '*'")
        self.expr = expr
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self._day = [h * 3600 + m * 60 for h in self.hours for m in self.minutes]

    def ticks(self, start: float, end: float) -> list:
        """Tick times t with start <= t < end (epoch seconds, UTC)."""
        out = []
        day = int(start // 86400) * 86400
        while day < end:
            for off in self._day:
                t = day + off
                if start <= t < end:
                    out.append(t)
            day += 86400
        return out

    def previous(self, t: float) -> float:
        """Latest tick strictly before t."""
        ticks = self.ticks(t - 2 * 86400, t)
        return ticks[-1]

    def interval_seconds(self) -> int:
        """Longest gap between consecutive ticks (the interval that matters for TTL math)."""
        day = self._day
        gaps = [b - a for a, b in zip(day, day[1:])] + [day[0] + 86400 - day[-1]]
        return max(gaps)


# ----------------------------------------------------------------- validation


def pickup_lag_seconds(cfg: dict):
    """How long after a new credential version the platform starts using it."""
    kind = get_path(cfg, "target.kind")
    if kind in ("azure_container_app", "azure_container_app_job", "azure_functions_aca"):
        return ACA_PICKUP_LAG
    if kind == "azure_functions_premium":
        return 0 if get_path(cfg, "pull_path.function_app_refresh", True) else PREMIUM_NO_REFRESH_LAG
    if kind == "azure_functions_flex":
        return None
    return 0


def ttl_math(cfg: dict) -> dict:
    try:
        interval = Cron(get_path(cfg, "pull_path.rotation_cron", "")).interval_seconds()
    except ValueError:
        return {}
    ttl = int(get_path(cfg, "pull_path.ttl_seconds", 0) or 0)
    lag = pickup_lag_seconds(cfg)
    lag_v = lag or 0
    # A token minted at t0 is valid until t0+ttl. After k missed runs the next
    # success is at t0+(k+1)*interval and the platform uses it lag later; that
    # must land before t0+ttl. Largest such k:
    survives = -(-(ttl - lag_v) // interval) - 2 if interval else 0
    alert_after = int(get_path(cfg, "pull_path.alert_after_seconds", 0) or 0)
    return {
        "rotation_interval_seconds": interval,
        "pickup_lag_seconds": lag,
        "pull_ttl_seconds": ttl,
        "min_ttl_seconds": int(get_path(cfg, "pull_path.min_ttl_seconds", 1800) or 0),
        "min_safe_ttl_seconds": 2 * interval + lag_v,
        "survives_missed_rotations": max(survives, 0),
        "alert_after_seconds": alert_after,
        # Time between the alert firing and the first possible pull failure,
        # counted from the last successful rotation.
        "alert_lead_seconds": ttl - lag_v - alert_after if alert_after else None,
    }


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
        return {"errors": errors, "warnings": warnings, "ttl_math": {}}
    cloud = cloud_of(kind)
    required = ("project", "region", "name") if cloud == "gcp" else ("subscription", "resource_group", "name")
    for f in required:
        if not get_path(cfg, f"target.{cloud}.{f}"):
            err(f"target.{cloud}.{f}", "required", f"{f} is required for {KINDS[kind]['label']}")
    if not get_path(cfg, f"jfrog.integration_names.{cloud}"):
        err(f"jfrog.integration_names.{cloud}", "required", "integration name is required")

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

    pp = get_path(cfg, "pull_path", {}) or {}
    math = {}
    if pp.get("enabled"):
        if kind == "azure_functions_flex":
            warn("pull_path.enabled", "flex_no_pull", "Flex Consumption runs code packages and never pulls images; the pull path is not needed")
        ttl = int(pp.get("ttl_seconds") or 0)
        if not 300 <= ttl <= 86400:
            err("pull_path.ttl_seconds", "pull_ttl_range", "pull TTL must be between 300 s and 24 h")
        try:
            Cron(pp.get("rotation_cron", ""))
        except ValueError as exc:
            err("pull_path.rotation_cron", "cron", str(exc))
        if not pp.get("registry_username"):
            err("pull_path.registry_username", "required", "registry username is required")
        if not pp.get("rotator_image"):
            warn("pull_path.rotator_image", "rotator_image_missing",
                 "push the rotator image to your own registry (Artifact Registry or ACR, not JFrog) and enter it here")
        math = ttl_math(cfg)
        if math:
            if ttl < math["min_safe_ttl_seconds"]:
                lag = math["pickup_lag_seconds"] or 0
                warn("pull_path.ttl_seconds", "pull_ttl_vs_rotation",
                     f"pull TTL {ttl}s is under 2 × rotation interval ({math['rotation_interval_seconds']}s) + pickup lag ({lag}s); "
                     f"one missed rotation can break pulls. Use at least {math['min_safe_ttl_seconds']}s")
            if ttl < math["min_ttl_seconds"]:
                warn("pull_path.ttl_seconds", "pull_ttl_below_min_ttl",
                     f"pull TTL {ttl}s is below the rotator's MIN_TTL_SECONDS ({math['min_ttl_seconds']}s): every rotation will be refused")
            alert_after = math["alert_after_seconds"]
            if alert_after and alert_after >= ttl - (math["pickup_lag_seconds"] or 0):
                warn("pull_path.alert_after_seconds", "alert_after_vs_ttl",
                     "the stale-rotation alert would fire after pulls already fail; lower alert_after_seconds below TTL − pickup lag")
    return {"errors": errors, "warnings": warnings, "ttl_math": math}


def schema() -> dict:
    """What the UI needs to render the form: kinds, defaults, and the field list."""
    return {
        "kinds": {k: {"cloud": v["cloud"], "label": v["label"]} for k, v in KINDS.items()},
        "defaults": {k: default_config(k) for k in KINDS},
        "pickup_lag_seconds": {"gcp": 0, "aca": ACA_PICKUP_LAG, "premium_no_refresh": PREMIUM_NO_REFRESH_LAG},
    }
