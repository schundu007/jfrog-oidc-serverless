"""Infrastructure the console can deploy and destroy (Terraform stacks under terraform/stacks).

Only for trying the credential provider where nothing exists yet. In production the targets are
already there and only the Serverless and Kubernetes consoles' configure steps run.

A stack with env/<env>.backend.hcl keeps remote state (init -backend-config) and its inputs in
env/<env>.tfvars; one without (ado-trust) keeps local state and terraform.tfvars.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS_DIR = REPO_ROOT / "terraform" / "stacks"

# id = the stack directory. Order is the order to deploy in; destroy in reverse.
CATALOGUE = (
    {"id": "ado-trust", "group": "Foundation", "title": "ADO trust"},
    {"id": "gcp-cloud-run", "group": "Google Cloud", "title": "Cloud Run job"},
    {"id": "gke-cluster", "group": "Google Cloud", "title": "GKE cluster"},
    {"id": "azure-container-apps", "group": "Azure", "title": "Container Apps job"},
    {"id": "aks-cluster", "group": "Azure", "title": "AKS cluster"},
)
IDS = tuple(s["id"] for s in CATALOGUE)
MAX_VARS = 64 * 1024
_SECRETISH = re.compile(r"(?im)^\s*\w*(token|password|secret|_key)\w*\s*=")


class StackError(ValueError):
    pass


def get(stack_id: str) -> dict:
    for s in CATALOGUE:
        if s["id"] == stack_id:
            return s
    raise StackError(f"unknown stack {stack_id!r}")


def stack_dir(stack_id: str, root: Path = STACKS_DIR) -> Path:
    get(stack_id)
    return Path(root) / stack_id


def vars_file(stack_id: str, env: str, root: Path = STACKS_DIR) -> Path:
    d = stack_dir(stack_id, root)
    return d / "env" / f"{env}.tfvars" if (d / "env" / f"{env}.backend.hcl").is_file() else d / "terraform.tfvars"


def tf_args(stack_id: str, env: str, root: Path = STACKS_DIR) -> tuple:
    """(init_args, plan_args), paths relative to the stack (terraform runs with -chdir)."""
    d = stack_dir(stack_id, root)
    init, plan = (), ()
    if (d / "env" / f"{env}.backend.hcl").is_file():
        init = ("-reconfigure", f"-backend-config=env/{env}.backend.hcl")
    vf = vars_file(stack_id, env, root)
    if vf.is_file():
        plan = (f"-var-file={vf.relative_to(d)}",)
    return init, plan


def jfrog_host(stack_id: str, env: str, root: Path = STACKS_DIR) -> str | None:
    """The JFrog host a stack's inputs name (jfrog_url), so the run gets a token for that host only."""
    m = re.search(r'(?m)^\s*jfrog_url\s*=\s*"https://([A-Za-z0-9.-]+)/?"', read_vars(stack_id, env, root))
    return m.group(1) if m else None


def read_vars(stack_id: str, env: str, root: Path = STACKS_DIR) -> str:
    p = vars_file(stack_id, env, root)
    return p.read_text() if p.is_file() else ""


def write_vars(stack_id: str, env: str, text: str, root: Path = STACKS_DIR) -> None:
    """Inputs are configuration, never credentials: tokens come from the environment at run time."""
    if not isinstance(text, str) or len(text.encode()) > MAX_VARS:
        raise StackError("inputs must be text under 64 KiB")
    if _SECRETISH.search(text):
        raise StackError("inputs must not hold tokens, passwords or keys")
    p = vars_file(stack_id, env, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text if text.endswith("\n") or not text else text + "\n")
    tmp.replace(p)


JFROG_TYPES = ("platform_", "artifactory_", "project_")


def needs_jfrog(stack_id: str, action: str, root: Path = STACKS_DIR) -> bool:
    """A destroy or status of a local-state stack needs a JFrog token only while its state holds
    JFrog resources. Remote state can't be read here, so those always get one."""
    if action not in ("plan-destroy", "status"):
        return True
    state = stack_dir(stack_id, root) / "terraform.tfstate"
    if not state.is_file():   # remote state (env/ files), or a local stack never deployed
        return (stack_dir(stack_id, root) / "env").is_dir()
    try:
        resources = json.loads(state.read_text()).get("resources") or []
    except (OSError, ValueError):
        return True
    return any(str(r.get("type", "")).startswith(JFROG_TYPES) for r in resources)
