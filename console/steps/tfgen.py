"""Terraform workspace rendering (spec §4.3).

One workspace per GCP project+region or per Azure subscription, shared by every
config that targets it: the pull path (rotator, AR remotes / Key Vault secret)
and the Entra audience app exist once per scope, so per-config workspaces would
create duplicates. Each config adds its own entries (runtime mapping, readers,
assignees) to the shared workspace under a file lock.

Files in .console/workspaces/<key>/:
  console.json            bookkeeping: shared settings + one entry per target (the source of truth)
  main.tf.json            fixed per cloud: references var.* only, never an operator value
  terraform.tfvars.json   every operator value, derived from console.json
  backend.tf.json         state backend

JSON syntax only (never string-templated HCL). Expressions in main.tf.json are
constants of this module, so no operator input can reach an expression. Nothing
secret is written: subjects are IDs, and JFROG_ACCESS_TOKEN reaches Terraform
through the environment at apply time.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from .. import model, redact

MODULES = Path(__file__).resolve().parents[2] / "terraform" / "modules"
GCP_AUDIENCE = "jfrog-gcp"

PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ARM_ID_RE = re.compile(r"^/subscriptions/[0-9a-f-]{36}/resourceGroups/[\w.()-]+/providers/[\w./()-]+$", re.I)
PREFIX_RE = {"gcp": re.compile(r"^[a-z][a-z0-9-]{1,13}$"), "azure": re.compile(r"^[a-z][a-z0-9-]{1,17}$")}
REPO_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REPO_FORMATS = {"DOCKER", "NPM", "PYTHON", "MAVEN"}

PROVIDERS = {
    "platform": {"source": "jfrog/platform", "version": ">= 2.2.1"},
    "google": {"source": "hashicorp/google", "version": ">= 6.13"},
    "azurerm": {"source": "hashicorp/azurerm", "version": ">= 4.23"},
    "azuread": {"source": "hashicorp/azuread", "version": ">= 2.47"},
    "random": {"source": "hashicorp/random", "version": ">= 3.5"},
}

WORKLOADS_TYPE = ("map(object({cloud = string, subject = string, username = optional(string), groups = list(string), "
                  "expires_in = number, description = string}))")
PULL_TYPE = ("object({enabled = bool, ttl_seconds = number, rotation_cron = string, min_ttl_seconds = number, "
             "alert_after_seconds = number, registry_username = string, rotator_image = string, groups = list(string)})")

# Shared settings: the config path each one comes from, for conflict messages.
SHARED_PATHS = {
    "jfrog_url": "jfrog.url", "reader_group": "jfrog.reader_group", "project_key": "jfrog.project_key",
    "integration_name": "jfrog.integration_names", "name_prefix": "pull_path.name_prefix",
    "state_backend": "state_backend",
}
PULL_PATHS = {
    "ttl_seconds": "pull_path.ttl_seconds", "rotation_cron": "pull_path.rotation_cron",
    "min_ttl_seconds": "pull_path.min_ttl_seconds", "alert_after_seconds": "pull_path.alert_after_seconds",
    "registry_username": "pull_path.registry_username", "rotator_image": "pull_path.rotator_image",
    "rotator_acr_id": "pull_path.rotator_acr_id", "key_vault_id": "target.azure.key_vault_id",
    "key_vault_uri": "target.azure.key_vault_uri", "environment_id": "target.azure.environment_id",
}


class TfgenError(ValueError):
    pass


# --------------------------------------------------------------------- locking


class FileLock:
    """Exclusive advisory lock on a file; works across threads and processes."""

    def __init__(self, path: Path, timeout: float = 120.0, on_wait=None):
        self.path, self.timeout, self.on_wait = Path(path), timeout, on_wait
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline, told = time.monotonic() + self.timeout, False
        while True:
            if _try_lock(self._fd):
                return self
            if not told and self.on_wait:
                self.on_wait(f"waiting for the lock on {self.path.parent.name} (another operation is running)")
                told = True
            if time.monotonic() > deadline:
                os.close(self._fd)
                self._fd = None
                raise TimeoutError(f"timed out after {self.timeout:.0f}s waiting for {self.path}")
            time.sleep(0.05)

    def __exit__(self, *exc):
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None


try:
    import fcntl

    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _try_lock(fd):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd):
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


# ------------------------------------------------------------------------ keys


def workspace_key(cfg: dict) -> str:
    kind = model.get_path(cfg, "target.kind")
    if kind not in model.KINDS:
        raise TfgenError(f"unknown target kind {kind!r}")
    if model.cloud_of(kind) == "gcp":
        project, region = model.get_path(cfg, "target.gcp.project", ""), model.get_path(cfg, "target.gcp.region", "")
        if not PROJECT_RE.match(project or ""):
            raise TfgenError(f"target.gcp.project {project!r} is not a valid project ID")
        if not REGION_RE.match(region or ""):
            raise TfgenError(f"target.gcp.region {region!r} is not a valid region")
        return f"gcp:{project}:{region}"
    sub = model.get_path(cfg, "target.azure.subscription", "")
    if not UUID_RE.match(sub or ""):
        raise TfgenError(f"target.azure.subscription {sub!r} is not a subscription ID")
    return f"azure:{sub.lower()}"


def workspace_dir(root: Path, key: str) -> Path:
    return Path(root) / "workspaces" / key.replace(":", "_")


def terraform_lock(ws_dir: Path, timeout: float = 3600.0, on_wait=None) -> FileLock:
    """Held for every terraform operation and every render of this workspace (spec §6.3)."""
    return FileLock(Path(ws_dir) / ".console.lock", timeout=timeout, on_wait=on_wait)


# ---------------------------------------------------------------------- render


def render(cfg: dict, root: Path, on_wait=None, modules: str | None = None) -> dict:
    """Add or update this config's target in its shared workspace and rewrite the files.

    modules: where main.tf.json points its module sources. Default: this checkout's
    terraform/modules (absolute). Delivery passes "../../modules" for a workspace
    committed under terraform/workspaces/.
    """
    return _update(cfg, Path(root), on_wait, remove=False, modules=modules)


def remove(cfg: dict, root: Path, on_wait=None) -> dict:
    """Drop this config's target from its workspace. Applying that plan removes its mapping."""
    return _update(cfg, Path(root), on_wait, remove=True)


def _update(cfg: dict, root: Path, on_wait, remove: bool, modules: str | None = None) -> dict:
    key = workspace_key(cfg)
    cloud = key.split(":", 1)[0]
    name = cfg.get("name")
    if not model.NAME_RE.match(str(name or "")):
        raise TfgenError("config name must be lowercase letters, digits and dashes")
    shared = target = None
    warnings: list = []
    if not remove:
        errors = model.validate(cfg)["errors"]
        if errors:
            raise TfgenError("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        shared, target, warnings = (_gcp_entry if cloud == "gcp" else _azure_entry)(cfg)

    ws = workspace_dir(root, key)
    # Global lock: the guards read every workspace. Then the workspace lock, so
    # files never change under a running terraform operation.
    with FileLock(root / "workspaces" / ".render.lock", on_wait=on_wait), terraform_lock(ws, on_wait=on_wait):
        state = _read_state(ws) or {"key": key, "cloud": cloud, "shared": {}, "targets": {}}
        if remove:
            state["targets"].pop(name, None)
        else:
            _merge_shared(state, shared, target)
            state["targets"][name] = target
            _guards(root, key, state)
            _check_complete(state)
        _write_workspace(ws, state, modules)
    return {"key": key, "dir": str(ws), "targets": sorted(state["targets"]), "warnings": warnings}


def _read_state(ws: Path):
    p = ws / "console.json"
    return json.loads(p.read_text()) if p.is_file() else None


def _merge_shared(state: dict, shared: dict, target: dict) -> None:
    cur = state["shared"]
    others = [t for n, t in state["targets"].items() if n != target["name"]]
    pull_others = [t for t in others if t.get("pull_enabled")]
    if target.get("runtime_enabled"):
        twin = [t["name"] for t in others if t.get("runtime_enabled") and t.get("subject") == target["subject"]]
        if twin:
            # One mapping per identity (AKS pilot rule): two mappings on one subject leave
            # the token's scope to mapping priority.
            raise TfgenError(f"{target['name']} and {', '.join(sorted(twin))} run as the same identity "
                             f"({target['subject']}); give each workload its own identity, or keep one config for it")
    for k, v in shared.items():
        if k == "pull":
            continue
        if k in ("resource_group_name", "location"):      # where the rotator lives: first target wins
            cur.setdefault(k, v)
            continue
        if others and k in cur and cur[k] != v:
            raise TfgenError(f"{SHARED_PATHS.get(k, k)} is {v!r} here but {cur[k]!r} in the shared workspace "
                             f"{state['key']} (used by {', '.join(sorted(t['name'] for t in others))}); make the configs agree")
        cur[k] = v
    if target.get("pull_enabled"):
        pull = cur.setdefault("pull", {})
        for k, v in shared["pull"].items():
            if v in (None, ""):
                continue
            if pull_others and pull.get(k) not in (None, "") and pull[k] != v:
                raise TfgenError(f"{PULL_PATHS.get(k, k)} is {v!r} here but {pull[k]!r} for "
                                 f"{', '.join(sorted(t['name'] for t in pull_others))} in the shared workspace "
                                 f"{state['key']}; the pull path is shared, so these must match")
            pull[k] = v
        repos = cur.setdefault("remote_repositories", {})
        for rid, r in (target.get("repositories") or {}).items():
            owner = [t["name"] for t in pull_others if rid in (t.get("repositories") or {})]
            if owner and rid in repos and repos[rid] != r:
                raise TfgenError(f"repository {rid} already proxies {repos[rid]['upstream_uri']} for {', '.join(owner)}; "
                                 "use another repository ID")
            repos[rid] = r


def _check_complete(state: dict) -> None:
    if not any(t.get("pull_enabled") for t in state["targets"].values()):
        return
    pull = state["shared"].get("pull") or {}
    need = ["rotator_image", "registry_username"] + (["key_vault_id", "environment_id"] if state["cloud"] == "azure" else [])
    missing = [PULL_PATHS[k] for k in need if not pull.get(k)]
    if missing:
        raise TfgenError(f"the pull path needs {', '.join(missing)}")


def _guards(root: Path, key: str, state: dict) -> None:
    """Collisions with other local workspaces that would only show up as apply errors."""
    me = state["shared"]
    mine = _mapping_names(state)
    for p in sorted((root / "workspaces").glob("*/console.json")):
        other = json.loads(p.read_text())
        if other.get("key") == key:
            continue
        o = other.get("shared", {})
        same_jfrog = o.get("jfrog_url") == me.get("jfrog_url")
        if same_jfrog and o.get("integration_name") == me.get("integration_name"):
            raise TfgenError(f"OIDC integration '{me['integration_name']}' is already managed by workspace {other['key']}; "
                             f"each workspace creates its own integration, so set jfrog.integration_names to a new name")
        clash = mine & _mapping_names(other) if same_jfrog else set()
        if clash:
            raise TfgenError(f"identity mapping {', '.join(sorted(clash))} already exists in workspace {other['key']}; "
                             "rename this config")
        if state["cloud"] == "gcp" and other.get("cloud") == "gcp" and key.split(":")[1] == other["key"].split(":")[1] \
                and o.get("name_prefix") == me.get("name_prefix"):
            raise TfgenError(f"pull_path.name_prefix '{me['name_prefix']}' is already used in project {key.split(':')[1]} "
                             f"by workspace {other['key']}; GCP resource names are project-wide, so pick another prefix")


def _mapping_names(state: dict) -> set:
    targets = state.get("targets", {})
    names = {f"{n}-runtime" for n, t in targets.items() if t.get("runtime_enabled")}
    if any(t.get("pull_enabled") for t in targets.values()):
        names.add(_pull_mapping_name(state["key"]))
    return names


def _pull_mapping_name(key: str) -> str:
    parts = key.split(":")
    return f"gcp-{parts[1]}-{parts[2]}-pull" if parts[0] == "gcp" else f"azure-{parts[1][:8]}-pull"


# ------------------------------------------------------------ per-cloud entries


def _common(cfg: dict, cloud: str) -> tuple:
    d = model.get_path(cfg, "target.discovered") or {}
    subject = d.get("mapping_subject")
    if not subject:
        raise TfgenError("no mapping subject: run Discover first (it reads the identity the mapping trusts)")
    if d.get("kind") and d["kind"] != model.get_path(cfg, "target.kind"):
        raise TfgenError(f"Discover found a {d['kind']} but the config says {model.get_path(cfg, 'target.kind')}; "
                         "run Discover again")
    prefix = model.get_path(cfg, "pull_path.name_prefix") or "jfrog"
    if not PREFIX_RE[cloud].match(prefix):
        raise TfgenError(f"pull_path.name_prefix {prefix!r}: lowercase letters, digits and dashes, "
                         f"{'2-14' if cloud == 'gcp' else '2-18'} characters")
    pp = model.get_path(cfg, "pull_path") or {}
    shared = {
        "jfrog_url": model.get_path(cfg, "jfrog.url").rstrip("/"),
        "reader_group": model.get_path(cfg, "jfrog.reader_group"),
        "project_key": model.get_path(cfg, "jfrog.project_key") or None,
        "integration_name": model.get_path(cfg, f"jfrog.integration_names.{cloud}"),
        "adopt_integration": bool(model.get_path(cfg, "jfrog.adopt_integration")),
        "name_prefix": prefix,
        "state_backend": _backend(cfg),
        "pull": {k: pp.get(k) for k in ("ttl_seconds", "rotation_cron", "min_ttl_seconds", "alert_after_seconds",
                                        "registry_username", "rotator_image")},
    }
    origin_problem = model.jfrog_origin_error(shared["jfrog_url"])
    if origin_problem:
        raise TfgenError(f"jfrog.url {origin_problem}")
    if not shared["reader_group"]:
        raise TfgenError("jfrog.reader_group is required")
    rt = model.get_path(cfg, "runtime") or {}
    target = {
        "name": cfg["name"], "kind": model.get_path(cfg, "target.kind"), "subject": str(subject),
        "runtime_enabled": bool(rt.get("enabled", True)), "runtime_ttl": int(rt.get("ttl_seconds") or 3600),
        "pull_enabled": bool(pp.get("enabled")),
    }
    return d, shared, target


def _require_rotator_image(cfg: dict, target: dict) -> None:
    if target["pull_enabled"] and not model.get_path(cfg, "pull_path.rotator_image"):
        raise TfgenError("pull_path.rotator_image is required for the pull path (an AR/ACR image, not one behind JFrog)")


def _gcp_entry(cfg: dict):
    d, shared, target = _common(cfg, "gcp")
    _require_rotator_image(cfg, target)
    warnings = []
    repos = {}
    for rid, r in (model.get_path(cfg, "pull_path.gcp_repositories") or {}).items():
        fmt, uri = str(r.get("format", "")).upper(), str(r.get("upstream_uri", "") or "").rstrip("/")
        if fmt == "DOCKER" and not uri and model.docker_host(cfg):
            uri = f"https://{model.docker_host(cfg)}"
        if not REPO_ID_RE.match(rid) or fmt not in REPO_FORMATS or not uri.startswith("https://"):
            raise TfgenError(f"pull_path.gcp_repositories.{rid}: needs an ID like jfrog-docker, a format in "
                             f"{sorted(REPO_FORMATS)} and an https upstream_uri")
        if fmt == "DOCKER":
            # A Docker upstream is the repo's dedicated host and nothing else; AR appends /v2/...
            if model.jfrog_origin_error(uri):
                raise TfgenError(f"pull_path.gcp_repositories.{rid}: a DOCKER upstream_uri is the repo's own host "
                                 f"(https://<server>-<repo-key>.jfrog.io), not {uri}")
            if uri == shared["jfrog_url"]:
                warnings.append(f"repository {rid} proxies the platform host {uri}: Docker pulls then use the "
                                "repository-path method, which failed for local repos on the AKS pilot. Point it at "
                                "the repo's dedicated host instead")
        repos[rid] = {"format": fmt, "upstream_uri": uri}
    if target["pull_enabled"] and not repos:
        raise TfgenError("pull_path.gcp_repositories: the pull path needs at least one remote repository")
    readers = []
    if target["kind"] == "gcp_run_function" and target["pull_enabled"]:
        if d.get("build_service_account"):
            readers.append(f"serviceAccount:{d['build_service_account']}")
        else:
            warnings.append("the function's build service account is unknown; grant it Artifact Registry Reader "
                            "on the remote repositories yourself")
    target.update(repositories=repos if target["pull_enabled"] else {}, reader_members=readers)
    return shared, target, warnings


def _azure_entry(cfg: dict):
    d, shared, target = _common(cfg, "azure")
    if target["kind"] == "azure_functions_flex":
        target["pull_enabled"] = False      # Flex runs code packages; Discover turns this off too
    _require_rotator_image(cfg, target)
    a = model.get_path(cfg, "target.azure") or {}
    pp = model.get_path(cfg, "pull_path") or {}
    kv, env = a.get("key_vault_id") or "", a.get("environment_id") or d.get("environment_id") or ""
    for path, v in (("target.azure.key_vault_id", kv), ("target.azure.environment_id", env)):
        if v and not ARM_ID_RE.match(v):
            raise TfgenError(f"{path} is not an ARM resource ID")
    kv_uri = a.get("key_vault_uri") or (f"https://{kv.rstrip('/').rsplit('/', 1)[-1]}.vault.azure.net/" if kv else "")
    acr = pp.get("rotator_acr_id") or None
    if acr and not ARM_ID_RE.match(acr):
        raise TfgenError("pull_path.rotator_acr_id is not an ARM resource ID")
    if acr and not str(pp.get("rotator_image", "")).split("/", 1)[0].endswith(".azurecr.io"):
        raise TfgenError("pull_path.rotator_acr_id is set but pull_path.rotator_image is not in an *.azurecr.io registry")
    if not d.get("location"):
        raise TfgenError("the target's location is unknown: run Discover again")
    shared.update(resource_group_name=a.get("resource_group"), location=d["location"])
    if target["pull_enabled"]:
        shared["pull"].update(key_vault_id=kv, key_vault_uri=kv_uri, environment_id=env, rotator_acr_id=acr)
    refresh = target["kind"] == "azure_functions_premium" and target["pull_enabled"] and pp.get("function_app_refresh", True)
    target.update(
        principal_id=d.get("principal_id") or str(d["mapping_subject"]),
        identity_type=d.get("identity_type"),
        function_app_id=(d.get("resource_id") or None) if refresh else None,
    )
    return shared, target, []


def _backend(cfg: dict) -> dict:
    b = model.get_path(cfg, "state_backend") or {"type": "local"}
    t = b.get("type", "local")
    allowed = {"local": (), "gcs": ("bucket", "prefix"),
               "azurerm": ("resource_group_name", "storage_account_name", "container_name", "key")}
    if t not in allowed:
        raise TfgenError(f"state_backend.type must be one of {sorted(allowed)}")
    if t == "local":
        return {"local": {"path": "terraform.tfstate"}}
    fields = {k: b.get(k) for k in allowed[t]}
    missing = [k for k, v in fields.items() if not v]
    if missing:
        raise TfgenError(f"state_backend ({t}) needs {', '.join(missing)}")
    if t == "azurerm":
        fields["use_azuread_auth"] = True   # state accounts allow Entra auth only, no account keys
    return {t: fields}


# ----------------------------------------------------------------------- files


def _write_workspace(ws: Path, state: dict, modules: str | None = None) -> None:
    ws.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ws, 0o700)
    files = {
        "console.json": state,
        "main.tf.json": _gcp_main(modules) if state["cloud"] == "gcp" else _azure_main(modules),
        "terraform.tfvars.json": _gcp_tfvars(state) if state["cloud"] == "gcp" else _azure_tfvars(state),
        "backend.tf.json": {"terraform": {"backend": state["shared"].get("state_backend") or {"local": {"path": "terraform.tfstate"}}}},
    }
    for name, data in files.items():
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        # main.tf.json is built from this module's constants only; the files
        # that carry operator values must not hold anything secret-looking.
        if name != "main.tf.json" and redact.text(text) != text:
            raise TfgenError(f"refusing to write {name}: a value looks like a secret")
        _atomic_write(ws / name, text)


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _workloads(state: dict, cloud: str) -> dict:
    group = state["shared"]["reader_group"]
    return {
        f"{n}-runtime": {"cloud": cloud, "subject": t["subject"], "username": None, "groups": [group],
                         "expires_in": t["runtime_ttl"], "description": f"{t['kind']} {n} (runtime exchange)"}
        for n, t in sorted(state["targets"].items()) if t["runtime_enabled"]
    }


def _pull_vars(state: dict) -> dict:
    s = state["shared"]
    p = s.get("pull") or {}
    return {
        "enabled": any(t["pull_enabled"] for t in state["targets"].values()),
        "ttl_seconds": int(p.get("ttl_seconds") or 14400),
        "rotation_cron": p.get("rotation_cron") or "23 * * * *",
        "min_ttl_seconds": int(p.get("min_ttl_seconds") or 1800),
        "alert_after_seconds": int(p.get("alert_after_seconds") or 10800),
        "registry_username": p.get("registry_username") or "",
        "rotator_image": p.get("rotator_image") or "",
        "groups": [s["reader_group"]],
    }


def _common_tfvars(state: dict, cloud: str) -> dict:
    s = state["shared"]
    return {
        "jfrog_url": s["jfrog_url"],
        "jfrog_project_key": s.get("project_key"),
        "jfrog_provider_name": s["integration_name"],
        "name_prefix": s["name_prefix"],
        "workloads": _workloads(state, cloud),
        "pull_path": _pull_vars(state),
        "pull_mapping_name": _pull_mapping_name(state["key"]),
        "adopt_jfrog_integration": bool(s.get("adopt_integration")),
    }


def _gcp_tfvars(state: dict) -> dict:
    _, project, region = state["key"].split(":")
    readers = sorted({m for t in state["targets"].values() if t["pull_enabled"] for m in t.get("reader_members", [])})
    return {
        **_common_tfvars(state, "gcp"),
        "jfrog_audience": GCP_AUDIENCE,
        "gcp_project_id": project,
        "gcp_region": region,
        "remote_repositories": dict(sorted((state["shared"].get("remote_repositories") or {}).items())),
        "reader_members": readers,
    }


def _azure_tfvars(state: dict) -> dict:
    s, targets = state["shared"], state["targets"]
    p = s.get("pull") or {}
    pulling = {n: t for n, t in targets.items() if t["pull_enabled"]}
    acr = p.get("rotator_acr_id") if pulling else None
    return {
        **_common_tfvars(state, "azure"),
        "azure_subscription_id": state["key"].split(":", 1)[1],
        "location": s["location"],
        "resource_group_name": s["resource_group_name"],
        "key_vault_id": p.get("key_vault_id") or None,
        "key_vault_uri": p.get("key_vault_uri") or None,
        "container_app_environment_id": p.get("environment_id") or None,
        "rotator_registry": {"server": p["rotator_image"].split("/", 1)[0], "acr_id": acr} if acr else None,
        "audience_assignees": {n: t["principal_id"] for n, t in sorted(targets.items()) if t["runtime_enabled"]},
        "secret_readers": {n: t["principal_id"] for n, t in sorted(pulling.items())},
        "function_app_ids": {n: t["function_app_id"] for n, t in sorted(pulling.items()) if t.get("function_app_id")},
    }


def _var(type_, default=..., description=None) -> dict:
    v = {"type": type_}
    if default is not ...:
        v["default"] = default
    if description:
        v["description"] = description
    return v


def _common_variables() -> dict:
    return {
        "jfrog_url": _var("string"),
        "jfrog_project_key": _var("string", None),
        "jfrog_provider_name": _var("string"),
        "name_prefix": _var("string"),
        "workloads": _var(WORKLOADS_TYPE, description="Runtime mappings, one per target: <config>-runtime"),
        "pull_path": _var(PULL_TYPE),
        "pull_mapping_name": _var("string"),
        "adopt_jfrog_integration": _var("bool", False),
    }


def _pull_workload_local(cloud: str, subject_expr: str, description: str) -> str:
    # A constant of this module: the mapping name and values come from var.*.
    return ("${{ for k in (var.pull_path.enabled ? [var.pull_mapping_name] : []) : k => { "
            f'cloud = "{cloud}", subject = {subject_expr}, username = var.pull_path.registry_username, '
            "groups = var.pull_path.groups, expires_in = var.pull_path.ttl_seconds, "
            f'description = "{description}" }} }}}}')


def _adopt_import(cloud: str) -> list:
    """An existing integration for the same issuer (preflight sets jfrog.adopt_integration)
    is imported into state instead of created again. A no-op once it is in state."""
    return [{"for_each": "${var.adopt_jfrog_integration ? toset([var.jfrog_provider_name]) : toset([])}",
             "to": f"module.jfrog_oidc.platform_oidc_configuration.{cloud}[0]", "id": "${each.value}"}]


def _module_root(modules: str | None) -> str:
    if modules is None:
        return str(MODULES)
    if modules not in ("../../modules",):   # a constant of this module, never operator input
        raise TfgenError(f"unsupported module root {modules!r}")
    return modules


def _gcp_main(modules: str | None = None) -> dict:
    mod = _module_root(modules)
    return {
        "import": _adopt_import("gcp"),
        "terraform": {"required_version": ">= 1.7",
                      "required_providers": {k: PROVIDERS[k] for k in ("platform", "google")}},
        "variable": {
            **_common_variables(),
            "jfrog_audience": _var("string"),
            "gcp_project_id": _var("string"),
            "gcp_region": _var("string"),
            "remote_repositories": _var("map(object({format = string, upstream_uri = string}))", {}),
            "reader_members": _var("list(string)", []),
        },
        "provider": {
            "platform": {"url": "${var.jfrog_url}"},
            "google": {"project": "${var.gcp_project_id}", "region": "${var.gcp_region}"},
        },
        "locals": {
            "pull_workloads": _pull_workload_local("gcp", "module.gcp_pull_path[0].rotator_unique_id",
                                                   "Artifact Registry upstream credential (rotated)"),
        },
        "module": {
            "jfrog_oidc": {
                "source": f"{mod}/jfrog-oidc",
                "gcp": {"provider_name": "${var.jfrog_provider_name}", "audience": "${var.jfrog_audience}"},
                "project_key": "${var.jfrog_project_key}",
                "workloads": "${merge(var.workloads, local.pull_workloads)}",
            },
            "gcp_pull_path": {
                "source": f"{mod}/gcp-pull-path",
                "count": "${var.pull_path.enabled ? 1 : 0}",
                "project_id": "${var.gcp_project_id}",
                "region": "${var.gcp_region}",
                "name_prefix": "${var.name_prefix}",
                "jfrog_url": "${var.jfrog_url}",
                "jfrog_provider_name": "${var.jfrog_provider_name}",
                "jfrog_audience": "${var.jfrog_audience}",
                "registry_username": "${var.pull_path.registry_username}",
                "remote_repositories": "${var.remote_repositories}",
                "reader_members": "${var.reader_members}",
                "rotator_image": "${var.pull_path.rotator_image}",
                "rotation_schedule": "${var.pull_path.rotation_cron}",
                "min_ttl_seconds": "${var.pull_path.min_ttl_seconds}",
                "alert_after_seconds": "${var.pull_path.alert_after_seconds}",
            },
        },
        "output": {
            "jfrog_provider_name": {"value": "${module.jfrog_oidc.gcp_provider_name}"},
            "jfrog_audience": {"value": "${module.jfrog_oidc.gcp_audience}"},
            "mapping_names": {"value": "${keys(merge(var.workloads, local.pull_workloads))}"},
            "repository_urls": {"value": "${one(module.gcp_pull_path[*].repository_urls)}"},
            "rotator_job_name": {"value": "${one(module.gcp_pull_path[*].rotator_job_name)}"},
            "first_run_command": {"value": "${one(module.gcp_pull_path[*].first_run_command)}"},
        },
    }


def _azure_main(modules: str | None = None) -> dict:
    mod = _module_root(modules)
    return {
        "import": _adopt_import("azure"),
        "terraform": {"required_version": ">= 1.7",
                      "required_providers": {k: PROVIDERS[k] for k in ("platform", "azurerm", "azuread", "random")}},
        "variable": {
            **_common_variables(),
            "azure_subscription_id": _var("string"),
            "location": _var("string"),
            "resource_group_name": _var("string"),
            "key_vault_id": _var("string", None),
            "key_vault_uri": _var("string", None),
            "container_app_environment_id": _var("string", None),
            "rotator_registry": _var("object({server = string, acr_id = string})", None),
            "audience_assignees": _var("map(string)", {}),
            "secret_readers": _var("map(string)", {}),
            "function_app_ids": _var("map(string)", {}),
        },
        "provider": {
            "platform": {"url": "${var.jfrog_url}"},
            "azurerm": {"features": {}, "subscription_id": "${var.azure_subscription_id}"},
            "azuread": {},
        },
        "locals": {
            "pull_workloads": _pull_workload_local("azure", "module.azure.rotator_principal_id",
                                                   "Container Apps / Functions registry credential (rotated)"),
        },
        "module": {
            "azure": {
                "source": f"{mod}/azure-identity-and-pull-path",
                "pull_path_enabled": "${var.pull_path.enabled}",
                "name_prefix": "${var.name_prefix}",
                "location": "${var.location}",
                "resource_group_name": "${var.resource_group_name}",
                "jfrog_url": "${var.jfrog_url}",
                "jfrog_provider_name": "${var.jfrog_provider_name}",
                "registry_username": "${var.pull_path.registry_username}",
                "key_vault_id": "${var.key_vault_id}",
                "key_vault_uri": "${var.key_vault_uri}",
                "container_app_environment_id": "${var.container_app_environment_id}",
                "rotator_image": "${var.pull_path.rotator_image}",
                "rotator_registry": "${var.rotator_registry}",
                "rotation_cron": "${var.pull_path.rotation_cron}",
                "min_ttl_seconds": "${var.pull_path.min_ttl_seconds}",
                "audience_assignees": "${var.audience_assignees}",
                "secret_readers": "${var.secret_readers}",
                "function_app_ids": "${var.function_app_ids}",
            },
            "jfrog_oidc": {
                "source": f"{mod}/jfrog-oidc",
                "azure": {"provider_name": "${var.jfrog_provider_name}", "tenant_id": "${module.azure.tenant_id}",
                          "audience_app_id": "${module.azure.audience_app_id}"},
                "project_key": "${var.jfrog_project_key}",
                "workloads": "${merge(var.workloads, local.pull_workloads)}",
            },
        },
        "output": {
            "jfrog_provider_name": {"value": "${module.jfrog_oidc.azure_provider_name}"},
            "audience_uri": {"value": "${module.azure.audience_uri}"},
            "tenant_id": {"value": "${module.azure.tenant_id}"},
            "mapping_names": {"value": "${keys(merge(var.workloads, local.pull_workloads))}"},
            "registry_username": {"value": "${var.pull_path.enabled ? var.pull_path.registry_username : null}"},
            "registry_secret_versionless_url": {"value": "${module.azure.registry_secret_versionless_url}"},
            "rotator_job_name": {"value": "${module.azure.rotator_job_name}"},
            "first_run_command": {"value": "${module.azure.first_run_command}"},
        },
    }
