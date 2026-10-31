"""Infrastructure the console can deploy and destroy (Terraform stacks under terraform/stacks).

Only for trying the credential provider where nothing exists yet. In production the targets are
already there and only the Serverless and Kubernetes consoles' configure steps run.

Nothing about a target is written into the stacks. Each stack's inputs are the variables its
variables.tf declares; the Infrastructure page shows them as a form and saves what is filled in
as JSON (<state root>/<env>/<stack>.tfvars.json, outside git), passed with -var-file. A variable
two stacks share (jfrog_url, gcp_project_id, ...) is prefilled from the last value saved for it.

Each cloud is isolated: its own foundation stack (gcp-foundation, azure-foundation) holds that
cloud's trust and state store, and its stacks' remote state is -backend-config derived from that
foundation's inputs only: gcs bucket <gcp_project_id>-tfstate; azurerm account st<ado_org>tfstate
in the ADO identity's resource group. JFrog's side of the ADO trust is its own stack. Foundation
stacks keep local state.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS_DIR = REPO_ROOT / "terraform" / "stacks"
STATE_ROOT = REPO_ROOT / ".console" / "setup"

# id = the stack directory. Order is the order to deploy in; destroy in reverse.
CATALOGUE = (
    {"id": "jfrog-ado-trust", "group": "JFrog", "title": "ADO pipelines trust"},
    {"id": "gcp-foundation", "group": "Google Cloud", "title": "Foundation (ADO trust, state)"},
    {"id": "gcp-cloud-run", "group": "Google Cloud", "title": "Cloud Run job"},
    {"id": "gke-cluster", "group": "Google Cloud", "title": "GKE cluster"},
    {"id": "azure-foundation", "group": "Azure", "title": "Foundation (ADO trust, state)"},
    {"id": "azure-container-apps", "group": "Azure", "title": "Container Apps job"},
    {"id": "aks-cluster", "group": "Azure", "title": "AKS cluster"},
)
IDS = tuple(s["id"] for s in CATALOGUE)
# The foundation whose inputs a remote backend's location derives from, by backend type.
FOUNDATION = {"gcs": "gcp-foundation", "azurerm": "azure-foundation"}
MAX_VARS = 64 * 1024
_SECRETISH = re.compile(r"(password|secret|(^|_)(api_)?key$|(^|_)token$)", re.I)
# Inputs a picker can offer live choices for (read-only CLI listings).
OPTION_SOURCES = {"gcp_project_id": "gcp_projects", "azure_subscription_id": "azure_subscriptions",
                  "azure_tenant_id": "azure_tenants"}


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


# -- variables.tf ----------------------------------------------------------------------------------

def _blocks(text: str):
    """(name, body) for each top-level `variable "name" { ... }`, skipping strings and heredocs."""
    for m in re.finditer(r'(?m)^variable\s+"([A-Za-z0-9_]+)"\s*\{', text):
        i, depth, n = m.end(), 1, len(text)
        while i < n and depth:
            c = text[i]
            if c == '"':
                i += 1
                while i < n and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif text.startswith("<<", i):
                h = re.match(r"<<-?([A-Za-z_]+)\n", text[i:])
                if h:
                    end = re.compile(rf"(?m)^\s*{h.group(1)}\s*$").search(text, i + h.end())
                    i = end.end() if end else n
                    continue
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        yield m.group(1), text[m.end():i - 1]


def _balance(s: str) -> int:
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    return sum(s.count(o) for o in "({[") - sum(s.count(c) for c in ")}]")


def _attrs(body: str) -> dict:
    """Top-level attributes of a variable block, as source text (nested blocks such as validation skipped)."""
    out, lines, i, depth = {}, body.splitlines(), 0, 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"\s*([a-z_]+)\s*=\s*(.*)$", line) if depth == 0 else None
        if m:
            name, expr = m.group(1), [m.group(2)]
            h = re.match(r"<<-?([A-Za-z_]+)\s*$", m.group(2))
            if h:
                i += 1
                while i < len(lines) and lines[i].strip() != h.group(1):
                    expr.append(lines[i])
                    i += 1
                out[name] = " ".join(x.strip() for x in expr[1:])
            else:
                bal = _balance(m.group(2))
                while bal > 0 and i + 1 < len(lines):
                    i += 1
                    expr.append(lines[i])
                    bal += _balance(lines[i])
                out[name] = "\n".join(expr).strip()
        else:
            depth += _balance(line)
        i += 1
    return out


def _literal(expr: str):
    """(ok, value) for a default the form can prefill: strings, numbers, bools, null, JSON-shaped {} / []."""
    e = expr.strip()
    if e == "null":
        return True, None
    if e in ("true", "false"):
        return True, e == "true"
    if "${" in e:
        return False, None
    try:
        return True, json.loads(e)
    except ValueError:
        return False, None


def variables(stack_id: str, root: Path = STACKS_DIR) -> list:
    """The stack's declared inputs: name, kind (string, number, bool, json), required, default, description."""
    out = []
    for f in sorted(stack_dir(stack_id, root).glob("*.tf")):
        for name, body in _blocks(f.read_text()):
            a = _attrs(body)
            t = a.get("type", "any").strip()
            desc = a.get("description", "").strip()
            v = {"name": name, "kind": t if t in ("string", "number", "bool") else "json", "type": t,
                 "required": "default" not in a,
                 "description": desc[1:-1] if desc.startswith('"') and desc.endswith('"') else desc,
                 "sensitive": a.get("sensitive", "").strip() == "true" or bool(_SECRETISH.search(name))}
            if "default" in a:
                ok, val = _literal(a["default"])
                if ok:
                    v["default"] = val
            if name in OPTION_SOURCES:
                v["options"] = OPTION_SOURCES[name]
            out.append(v)
    return out


def backend_kind(stack_id: str, root: Path = STACKS_DIR) -> str | None:
    p = stack_dir(stack_id, root) / "backend.tf"
    m = re.search(r'backend\s+"([a-z0-9]+)"', p.read_text()) if p.is_file() else None
    return m.group(1) if m else None


# -- saved inputs ------------------------------------------------------------------------------------

def inputs_file(stack_id: str, env: str, state_root: Path = STATE_ROOT) -> Path:
    get(stack_id)
    return Path(state_root) / env / f"{stack_id}.tfvars.json"


def _shared_file(env: str, state_root: Path) -> Path:
    return Path(state_root) / env / "shared.json"


def _load(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def read_inputs(stack_id: str, env: str, state_root: Path = STATE_ROOT) -> dict:
    return _load(inputs_file(stack_id, env, state_root))


def form(stack_id: str, env: str, root: Path = STACKS_DIR, state_root: Path = STATE_ROOT) -> dict:
    """The stack's variables with what to show: the saved value, else the last value saved for a
    shared name, else the default."""
    saved, shared = read_inputs(stack_id, env, state_root), _load(_shared_file(env, state_root))
    fields = []
    for v in variables(stack_id, root):
        if v["sensitive"]:
            continue   # credentials come from the environment at run time, never from the form
        f = dict(v)
        if v["name"] in saved:
            f["current"], f["source"] = saved[v["name"]], "saved"
        elif v["name"] in shared:
            f["current"], f["source"] = shared[v["name"]], "shared"
        elif "default" in v:
            f["current"], f["source"] = v["default"], "default"
        fields.append(f)
    return {"fields": fields, "saved": bool(saved), "missing": missing(stack_id, env, root, state_root)}


def _coerce(v: dict, raw):
    k = v["kind"]
    if k == "string":
        if not isinstance(raw, str):
            raise StackError(f"{v['name']}: text expected")
        return raw.strip()
    if k == "number":
        if isinstance(raw, bool):
            raise StackError(f"{v['name']}: a number expected")
        if isinstance(raw, (int, float)):
            return raw
        try:
            return float(raw) if "." in str(raw) else int(raw)
        except (TypeError, ValueError):
            raise StackError(f"{v['name']}: a number expected") from None
    if k == "bool":
        if not isinstance(raw, bool):
            raise StackError(f"{v['name']}: true or false expected")
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            raise StackError(f"{v['name']}: JSON expected") from None
    return raw


def write_inputs(stack_id: str, env: str, values: dict, root: Path = STACKS_DIR, state_root: Path = STATE_ROOT) -> dict:
    """Save the form. Only declared, non-sensitive variables; a blank optional one falls back to
    its default. Inputs are configuration, never credentials."""
    if not isinstance(values, dict) or len(json.dumps(values).encode()) > MAX_VARS:
        raise StackError("inputs must be an object under 64 KiB")
    declared = {v["name"]: v for v in variables(stack_id, root)}
    out = {}
    for name, raw in values.items():
        v = declared.get(name)
        if v is None:
            raise StackError(f"{name} is not an input of {stack_id}")
        if v["sensitive"]:
            raise StackError("inputs must not hold tokens, passwords or keys")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        out[name] = _coerce(v, raw)
    _write(inputs_file(stack_id, env, state_root), out)
    others = {x["name"] for s in IDS if s != stack_id for x in variables(s, root)}
    shared = _load(_shared_file(env, state_root))
    shared.update({k: val for k, val in out.items() if k in others})
    _write(_shared_file(env, state_root), shared)
    return out


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


def missing(stack_id: str, env: str, root: Path = STACKS_DIR, state_root: Path = STATE_ROOT) -> list:
    saved = read_inputs(stack_id, env, state_root)
    return [v["name"] for v in variables(stack_id, root) if v["required"] and not v["sensitive"] and v["name"] not in saved]


# -- terraform arguments -------------------------------------------------------------------------------

def foundation(kind: str, env: str, state_root: Path = STATE_ROOT) -> dict:
    """What a backend's location derives from: the saved inputs of that cloud's foundation only."""
    f = read_inputs(FOUNDATION[kind], env, state_root) if kind in FOUNDATION else {}
    return {"gcp_project_id": f.get("gcp_project_id"), "azure_subscription_id": f.get("azure_subscription_id"),
            "ado_org": f.get("ado_org"), "state_resource_group": (f.get("azure_identity") or {}).get("resource_group")}


def backend_config(kind: str, stack_id: str, env: str, found: dict) -> tuple:
    """-backend-config pairs for a gcs or azurerm backend, matching the stores ADO trust creates."""
    need = {"gcs": ("gcp_project_id",), "azurerm": ("azure_subscription_id", "ado_org", "state_resource_group")}.get(kind)
    if need is None:
        raise StackError(f"{stack_id}: backend {kind!r} is not supported")
    gaps = [k for k in need if not found.get(k)]
    if gaps:
        raise StackError(f"save the {FOUNDATION[kind]} inputs first ({', '.join(gaps)}): its state store holds this stack's state")
    if kind == "gcs":
        pairs = {"bucket": f"{found['gcp_project_id']}-tfstate", "prefix": f"{stack_id}/{env}"}
    else:
        pairs = {"subscription_id": found["azure_subscription_id"], "resource_group_name": found["state_resource_group"],
                 "storage_account_name": ("st" + found["ado_org"].replace("-", "") + "tfstate")[:24],
                 "container_name": "tfstate", "key": f"{stack_id}/{env}.tfstate", "use_azuread_auth": "true"}
    return tuple(f"-backend-config={k}={v}" for k, v in pairs.items())


def tf_args(stack_id: str, env: str, root: Path = STACKS_DIR, state_root: Path = STATE_ROOT) -> tuple:
    """(init_args, plan_args). Refuses while a required input is still empty."""
    gaps = missing(stack_id, env, root, state_root)
    if gaps:
        raise StackError(f"fill in the inputs first: {', '.join(gaps)}")
    init = ()
    kind = backend_kind(stack_id, root)
    if kind:
        init = ("-reconfigure", *backend_config(kind, stack_id, env, foundation(kind, env, state_root)))
    return init, (f"-var-file={inputs_file(stack_id, env, state_root)}",)


def jfrog_host(stack_id: str, env: str, state_root: Path = STATE_ROOT) -> str | None:
    """The JFrog host a stack's inputs name (jfrog_url), so the run gets a token for that host only."""
    m = re.fullmatch(r"https://([A-Za-z0-9.-]+)/?", str(read_inputs(stack_id, env, state_root).get("jfrog_url") or ""))
    return m.group(1) if m else None


JFROG_TYPES = ("platform_", "artifactory_", "project_")


def needs_jfrog(stack_id: str, action: str, root: Path = STACKS_DIR) -> bool:
    """A destroy or status of a local-state stack needs a JFrog token only while its state holds
    JFrog resources. Remote state can't be read here, so those always get one."""
    if action not in ("plan-destroy", "status"):
        return True
    state = stack_dir(stack_id, root) / "terraform.tfstate"
    if not state.is_file():   # remote state, or a local stack never deployed
        return backend_kind(stack_id, root) is not None
    try:
        resources = json.loads(state.read_text()).get("resources") or []
    except (OSError, ValueError):
        return True
    return any(str(r.get("type", "")).startswith(JFROG_TYPES) for r in resources)


def main(argv=None) -> int:
    """Pipelines: python3 -m setup_console.stacks backend-args <stack> <env> <foundation.json>
    prints one -backend-config=k=v per line (nothing for a local-state stack)."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4 or argv[0] != "backend-args":
        print(main.__doc__, file=sys.stderr)
        return 2
    _, sid, env, found = argv
    kind = backend_kind(sid)
    if kind:
        print("\n".join(backend_config(kind, sid, env, json.loads(Path(found).read_text()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
