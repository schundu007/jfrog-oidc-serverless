"""Terraform workspace rendering (spec §4.3).

One workspace per GCP project+region or per Azure subscription, shared by every
config that targets it: the JFrog integration and the Entra audience app exist
once per scope, so per-config workspaces would create duplicates. Each config
adds its own entries (runtime mapping, dedicated identity, assignees) to the shared workspace under a file lock.

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
PREFIX_RE = {"gcp": re.compile(r"^[a-z][a-z0-9-]{1,13}$"), "azure": re.compile(r"^[a-z][a-z0-9-]{1,17}$"), "openshift": re.compile(r"^[a-z][a-z0-9-]{1,17}$")}
ROLE_RE = re.compile(r"^(projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/)?roles/[A-Za-z0-9_.]{3,64}$")

PROVIDERS = {
    "platform": {"source": "jfrog/platform", "version": ">= 2.2.1"},
    "google": {"source": "hashicorp/google", "version": ">= 6.13"},
    "azurerm": {"source": "hashicorp/azurerm", "version": ">= 4.23"},
    "azuread": {"source": "hashicorp/azuread", "version": ">= 2.47"},
    "random": {"source": "hashicorp/random", "version": ">= 3.5"},
    "kubernetes": {"source": "hashicorp/kubernetes", "version": ">= 2.30"},
}
K8S_AUDIENCE = "jfrog-k8s"
ISSUER_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~/-]*)?$")
K8S_SUBJECT_RE = re.compile(r"^system:serviceaccount:[a-z0-9-]{1,63}:[a-z0-9.-]{1,253}$")

WORKLOADS_TYPE = ("map(object({cloud = string, subject = string, username = optional(string), groups = list(string), "
                  "expires_in = number, description = string}))")
# Shared settings: the config path each one comes from, for conflict messages.
SHARED_PATHS = {
    "jfrog_url": "jfrog.url", "reader_group": "jfrog.reader_group", "project_key": "jfrog.project_key",
    "integration_name": "jfrog.integration_names", "name_prefix": "name_prefix",
    "state_backend": "state_backend",
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
    if model.cloud_of(kind) == "openshift":
        # One workspace per cluster: one service-account issuer, one JFrog integration.
        cluster = str(model.get_path(cfg, "target.openshift.cluster", "") or "")
        if not model.CLUSTER_RE.match(cluster):
            raise TfgenError(f"target.openshift.cluster {cluster!r}: lowercase letters, digits and dashes")
        return f"openshift:{cluster}"
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


def render(cfg: dict, root: Path, on_wait=None, modules: str | None = None, drop=()) -> dict:
    """Add or update this config's target in its shared workspace and rewrite the files.

    modules: where main.tf.json points its module sources. Default: this checkout's
    terraform/modules (absolute). Delivery passes "../../modules" for a workspace
    committed under terraform/workspaces/.
    drop: other targets to take out of the workspace (their workload no longer exists).
    """
    return _update(cfg, Path(root), on_wait, remove=False, modules=modules, drop=drop)


def remove(cfg: dict, root: Path, on_wait=None) -> dict:
    """Drop this config's target from its workspace. Applying that plan removes its mapping."""
    return _update(cfg, Path(root), on_wait, remove=True)


def _update(cfg: dict, root: Path, on_wait, remove: bool, modules: str | None = None, drop=()) -> dict:
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
        shared, target, warnings = {"gcp": _gcp_entry, "azure": _azure_entry,
                                    "openshift": _openshift_entry}[cloud](cfg)

    ws = workspace_dir(root, key)
    # Global lock: the guards read every workspace. Then the workspace lock, so
    # files never change under a running terraform operation.
    with FileLock(root / "workspaces" / ".render.lock", on_wait=on_wait), terraform_lock(ws, on_wait=on_wait):
        state = _read_state(ws) or {"key": key, "cloud": cloud, "shared": {}, "targets": {}}
        for gone in drop:
            if gone != name:
                state["targets"].pop(gone, None)
        if not state["targets"] or set(state["targets"]) == {name}:
            # The first target's resource group and location only stand while it does.
            for k in ("resource_group_name", "location"):
                state["shared"].pop(k, None)
        if remove:
            state["targets"].pop(name, None)
        else:
            _merge_shared(state, shared, target)
            state["targets"][name] = target
            _guards(root, key, state)
        _write_workspace(ws, state, modules)
    return {"key": key, "dir": str(ws), "targets": sorted(state["targets"]), "warnings": warnings}


def read_state(root: Path, key: str):
    """The workspace's console.json for this key, or None."""
    return _read_state(workspace_dir(Path(root), key))


def _read_state(ws: Path):
    p = ws / "console.json"
    return json.loads(p.read_text()) if p.is_file() else None


def _merge_shared(state: dict, shared: dict, target: dict) -> None:
    cur = state["shared"]
    others = [t for n, t in state["targets"].items() if n != target["name"]]
    if target.get("runtime_enabled") and target["subject"]:
        twin = [t["name"] for t in others if t.get("runtime_enabled") and t.get("subject") == target["subject"]]
        if twin:
            # One mapping per identity (AKS pilot rule): two mappings on one subject leave
            # the token's scope to mapping priority.
            raise TfgenError(f"{target['name']} and {', '.join(sorted(twin))} run as the same identity "
                             f"({target['subject']}); give each workload its own identity, or keep one config for it")
    for k, v in shared.items():
        if k in ("resource_group_name", "location", "kube_context"):   # first target wins
            cur.setdefault(k, v)
            continue
        if others and k in cur and cur[k] != v:
            raise TfgenError(f"{SHARED_PATHS.get(k, k)} is {v!r} here but {cur[k]!r} in the shared workspace "
                             f"{state['key']} (used by {', '.join(sorted(t['name'] for t in others))}); make the configs agree")
        cur[k] = v
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
def _mapping_names(state: dict) -> set:
    targets = state.get("targets", {})
    return {f"{n}-runtime" for n, t in targets.items() if t.get("runtime_enabled")}


# ------------------------------------------------------------ per-cloud entries


def _common(cfg: dict, cloud: str) -> tuple:
    d = model.get_path(cfg, "target.discovered") or {}
    subject = d.get("mapping_subject")
    dedicated = d.get("dedicated_identity") or None
    if not subject and not dedicated:
        raise TfgenError("no mapping subject: run Discover first (it reads the identity the mapping trusts)")
    if dedicated and dedicated != model.dedicated_identity_name(cfg):
        raise TfgenError(f"the dedicated identity {dedicated!r} does not belong to {cfg['name']}: run Discover again")
    if d.get("kind") and d["kind"] != model.get_path(cfg, "target.kind"):
        raise TfgenError(f"Discover found a {d['kind']} but the config says {model.get_path(cfg, 'target.kind')}; "
                         "run Discover again")
    prefix = model.get_path(cfg, "name_prefix") or "jfrog"
    if not PREFIX_RE[cloud].match(prefix):
        raise TfgenError(f"name_prefix {prefix!r}: lowercase letters, digits and dashes, "
                         f"{'2-14' if cloud == 'gcp' else '2-18'} characters")
    shared = {
        "jfrog_url": model.get_path(cfg, "jfrog.url").rstrip("/"),
        "reader_group": model.get_path(cfg, "jfrog.reader_group"),
        "project_key": model.get_path(cfg, "jfrog.project_key") or None,
        "integration_name": model.get_path(cfg, f"jfrog.integration_names.{cloud}"),
        "adopt_integration": bool(model.get_path(cfg, "jfrog.adopt_integration")),
        "name_prefix": prefix,
        "state_backend": _backend(cfg),
    }
    origin_problem = model.jfrog_origin_error(shared["jfrog_url"])
    if origin_problem:
        raise TfgenError(f"jfrog.url {origin_problem}")
    if not shared["reader_group"]:
        raise TfgenError("jfrog.reader_group is required")
    rt = model.get_path(cfg, "runtime") or {}
    target = {
        # An empty subject is the dedicated identity's ID, known only once the workspace creates it.
        "name": cfg["name"], "kind": model.get_path(cfg, "target.kind"), "subject": str(subject or ""),
        "dedicated_identity": dedicated,
        "runtime_enabled": bool(rt.get("enabled", True)), "runtime_ttl": int(rt.get("ttl_seconds") or 3600),
    }
    return d, shared, target


def _gcp_entry(cfg: dict):
    d, shared, target = _common(cfg, "gcp")
    roles = [r for r in (d.get("dedicated_service_account_roles") or []) if ROLE_RE.match(str(r))]
    target.update(dedicated_roles=sorted(roles) if target["dedicated_identity"] else [])
    return shared, target, []


def _azure_entry(cfg: dict):
    d, shared, target = _common(cfg, "azure")
    a = model.get_path(cfg, "target.azure") or {}
    if not d.get("location"):
        raise TfgenError("the target's location is unknown: run Discover again")
    shared.update(resource_group_name=a.get("resource_group"), location=d["location"])
    target.update(
        principal_id=d.get("principal_id") or str(d.get("mapping_subject") or ""),
        identity_resource_group=a.get("resource_group"), identity_location=d["location"],
        identity_type=d.get("identity_type"),
    )
    return shared, target, []


def _issuer(d: dict, what: str) -> str:
    iss = str(d.get("issuer") or "").rstrip("/")
    if not ISSUER_RE.match(iss):
        raise TfgenError(f"the {what} issuer is unknown or not an https URL ({iss!r}): run Discover again")
    return iss


def _openshift_entry(cfg: dict):
    d, shared, target = _common(cfg, "openshift")
    if target["subject"] and not K8S_SUBJECT_RE.match(target["subject"]):
        raise TfgenError(f"mapping subject {target['subject']!r} is not system:serviceaccount:<ns>:<name>")
    o = model.get_path(cfg, "target.openshift") or {}
    shared.update(issuer=_issuer(d, "cluster service-account"), kube_context=o.get("context"))
    target.update(namespace=o.get("namespace"))
    return shared, target, []


def _backend(cfg: dict) -> dict:
    b = model.get_path(cfg, "state_backend") or {"type": "local"}
    t = b.get("type", "local")
    allowed = {"local": (), "gcs": ("bucket", "prefix"),
               "azurerm": ("resource_group_name", "storage_account_name", "container_name", "key"), "kubernetes": ("secret_suffix",)}
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
    if t == "kubernetes":                   # state in a Secret (with a Lease lock) in the cluster itself
        fields.update({k: b[k] for k in ("namespace", "config_context") if b.get(k)})
    return {t: fields}


# ----------------------------------------------------------------------- files


def _write_workspace(ws: Path, state: dict, modules: str | None = None) -> None:
    ws.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ws, 0o700)
    main = {"gcp": _gcp_main, "azure": _azure_main, "openshift": _openshift_main}[state["cloud"]]
    tfvars = {"gcp": _gcp_tfvars, "azure": _azure_tfvars, "openshift": _openshift_tfvars}[state["cloud"]]
    files = {
        "console.json": state,
        "main.tf.json": main(modules),
        "terraform.tfvars.json": tfvars(state),
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


def _common_tfvars(state: dict, cloud: str) -> dict:
    s = state["shared"]
    return {
        "jfrog_url": s["jfrog_url"],
        "jfrog_project_key": s.get("project_key"),
        "jfrog_provider_name": s["integration_name"],
        "name_prefix": s["name_prefix"],
        "workloads": _workloads(state, cloud),
        "adopt_jfrog_integration": bool(s.get("adopt_integration")),
    }


def _gcp_tfvars(state: dict) -> dict:
    _, project, region = state["key"].split(":")
    return {
        **_common_tfvars(state, "gcp"),
        "jfrog_audience": GCP_AUDIENCE,
        "gcp_project_id": project,
        "gcp_region": region,
        "dedicated_service_accounts": {n: t["dedicated_identity"] for n, t in sorted(state["targets"].items())
                                       if t.get("dedicated_identity")},
        "dedicated_service_account_roles": {n: t["dedicated_roles"] for n, t in sorted(state["targets"].items())
                                            if t.get("dedicated_identity") and t.get("dedicated_roles")},
    }


def _azure_tfvars(state: dict) -> dict:
    s, targets = state["shared"], state["targets"]
    return {
        **_common_tfvars(state, "azure"),
        "azure_subscription_id": state["key"].split(":", 1)[1],
        "location": s["location"],
        "resource_group_name": s["resource_group_name"],
        "audience_assignees": {n: t["principal_id"] for n, t in sorted(targets.items()) if t["runtime_enabled"]},
        "dedicated_identities": {n: {"name": t["dedicated_identity"], "resource_group_name": t["identity_resource_group"],
                                     "location": t["identity_location"]}
                                 for n, t in sorted(targets.items()) if t.get("dedicated_identity")},
    }


def _openshift_tfvars(state: dict) -> dict:
    s, targets = state["shared"], state["targets"]
    return {
        **_common_tfvars(state, "openshift"),
        "kube_context": s.get("kube_context") or None,
        "jfrog_issuer": s["issuer"],
        "jfrog_audience": K8S_AUDIENCE,
        "dedicated_service_accounts": {n: {"name": t["dedicated_identity"], "namespace": t["namespace"]}
                                       for n, t in sorted(targets.items()) if t.get("dedicated_identity")},
    }


# Runtime mappings; a target on a dedicated identity takes that identity's ID as its subject.
RUNTIME_WORKLOADS = ("${{ for k, w in var.workloads : k => merge(w, "
                     "{ subject = lookup(local.dedicated_subjects, trimsuffix(k, \"-runtime\"), w.subject) }) }}")


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
        "adopt_jfrog_integration": _var("bool", False),
    }


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
            "dedicated_service_accounts": _var("map(string)", {}, "Existing apps on a default account: <config> => new SA ID"),
            "dedicated_service_account_roles": _var("map(list(string))", {},
                                                    "<config> => project roles the new SA takes over from the default one"),
        },
        "resource": {
            "google_service_account": {
                "dedicated": {
                    "for_each": "${var.dedicated_service_accounts}",
                    "project": "${var.gcp_project_id}",
                    "account_id": "${each.value}",
                    "display_name": "JFrog OIDC: ${each.key}",
                    "description": "Dedicated runtime identity for ${each.key}, created by the JFrog OIDC console",
                },
            },
            "google_project_iam_member": {
                "dedicated": {
                    "for_each": "${local.dedicated_role_grants}",
                    "project": "${var.gcp_project_id}",
                    "role": "${each.value.role}",
                    "member": "serviceAccount:${google_service_account.dedicated[each.value.config].email}",
                },
            },
        },
        "provider": {
            "platform": {"url": "${var.jfrog_url}"},
            "google": {"project": "${var.gcp_project_id}", "region": "${var.gcp_region}"},
        },
        "locals": {
            "dedicated_subjects": "${{ for n, sa in google_service_account.dedicated : n => sa.unique_id }}",
            "dedicated_role_grants": ("${merge({}, [for n, roles in var.dedicated_service_account_roles : "
                                      "{ for r in roles : \"${n} ${r}\" => { config = n, role = r } } "
                                      "if contains(keys(var.dedicated_service_accounts), n)]...)}"),
            "runtime_workloads": RUNTIME_WORKLOADS,
        },
        "module": {
            "jfrog_oidc": {
                "source": f"{mod}/jfrog-oidc",
                "gcp": {"provider_name": "${var.jfrog_provider_name}", "audience": "${var.jfrog_audience}"},
                "project_key": "${var.jfrog_project_key}",
                "workloads": "${local.runtime_workloads}",
            },
        },
        "output": {
            "jfrog_provider_name": {"value": "${module.jfrog_oidc.gcp_provider_name}"},
            "jfrog_audience": {"value": "${module.jfrog_oidc.gcp_audience}"},
            "mapping_names": {"value": "${keys(local.runtime_workloads)}"},
            "dedicated_service_accounts": {"value": "${{ for n, sa in google_service_account.dedicated : n => sa.email }}"},
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
            "audience_assignees": _var("map(string)", {}),
            "dedicated_identities": _var("map(object({name = string, resource_group_name = string, location = string}))", {},
                                         "Existing apps with no managed identity: <config> => new user-assigned identity"),
        },
        "resource": {
            "azurerm_user_assigned_identity": {
                "dedicated": {
                    "for_each": "${var.dedicated_identities}",
                    "name": "${each.value.name}",
                    "resource_group_name": "${each.value.resource_group_name}",
                    "location": "${each.value.location}",
                },
            },
        },
        "provider": {
            "platform": {"url": "${var.jfrog_url}"},
            "azurerm": {"features": {}, "subscription_id": "${var.azure_subscription_id}"},
            "azuread": {},
        },
        "locals": {
            "dedicated_subjects": "${{ for n, i in azurerm_user_assigned_identity.dedicated : n => i.principal_id }}",
            "runtime_workloads": RUNTIME_WORKLOADS,
            "audience_assignees": "${{ for n, p in var.audience_assignees : n => lookup(local.dedicated_subjects, n, p) }}",
        },
        "module": {
            "azure": {
                "source": f"{mod}/azure-identity",
                "name_prefix": "${var.name_prefix}",
                "audience_assignees": "${local.audience_assignees}",
            },
            "jfrog_oidc": {
                "source": f"{mod}/jfrog-oidc",
                "azure": {"provider_name": "${var.jfrog_provider_name}", "tenant_id": "${module.azure.tenant_id}",
                          "audience_app_id": "${module.azure.audience_app_id}"},
                "project_key": "${var.jfrog_project_key}",
                "workloads": "${local.runtime_workloads}",
            },
        },
        "output": {
            "jfrog_provider_name": {"value": "${module.jfrog_oidc.azure_provider_name}"},
            "audience_uri": {"value": "${module.azure.audience_uri}"},
            "tenant_id": {"value": "${module.azure.tenant_id}"},
            "mapping_names": {"value": "${keys(local.runtime_workloads)}"},
            "dedicated_identities": {"value": "${{ for n, i in azurerm_user_assigned_identity.dedicated : "
                                              "n => { id = i.id, client_id = i.client_id, principal_id = i.principal_id } }}"},
        },
    }


def _openshift_main(modules: str | None = None) -> dict:
    mod = _module_root(modules)
    return {
        "import": _adopt_import("openshift"),
        "terraform": {"required_version": ">= 1.7",
                      "required_providers": {k: PROVIDERS[k] for k in ("platform", "kubernetes")}},
        "variable": {
            **_common_variables(),
            "kube_context": _var("string", None, "kube context; null = the kubeconfig's current context"),
            "kubeconfig_path": _var("string", "~/.kube/config"),
            "jfrog_issuer": _var("string", description="The cluster's service-account issuer"),
            "jfrog_audience": _var("string"),
            "dedicated_service_accounts": _var("map(object({name = string, namespace = string}))", {},
                                               "Knative services on the namespace's default SA: <config> => new SA"),
        },
        "provider": {
            "platform": {"url": "${var.jfrog_url}"},
            "kubernetes": {"config_path": "${var.kubeconfig_path}", "config_context": "${var.kube_context}"},
        },
        "resource": {
            "kubernetes_service_account_v1": {
                "dedicated": {
                    "for_each": "${var.dedicated_service_accounts}",
                    "metadata": {"name": "${each.value.name}", "namespace": "${each.value.namespace}",
                                 "labels": {"app.kubernetes.io/managed-by": "jfrog-oidc-console"},
                                 "annotations": {"jfrog-oidc/workload": "${each.key}"}},
                },
            },
        },
        "locals": {
            "dedicated_subjects": ("${{ for n, sa in kubernetes_service_account_v1.dedicated : n => "
                                   "\"system:serviceaccount:${sa.metadata[0].namespace}:${sa.metadata[0].name}\" }}"),
            "runtime_workloads": RUNTIME_WORKLOADS,
        },
        "module": {
            "jfrog_oidc": {
                "source": f"{mod}/jfrog-oidc",
                "openshift": {"provider_name": "${var.jfrog_provider_name}", "issuer": "${var.jfrog_issuer}",
                              "audience": "${var.jfrog_audience}"},
                "project_key": "${var.jfrog_project_key}",
                "workloads": "${local.runtime_workloads}",
            },
        },
        "output": {
            "jfrog_provider_name": {"value": "${module.jfrog_oidc.openshift_provider_name}"},
            "jfrog_audience": {"value": "${module.jfrog_oidc.openshift_audience}"},
            "mapping_names": {"value": "${keys(local.runtime_workloads)}"},
            "dedicated_service_accounts": {"value": "${{ for n, sa in kubernetes_service_account_v1.dedicated : "
                                                    "n => sa.metadata[0].name }}"},
        },
    }
