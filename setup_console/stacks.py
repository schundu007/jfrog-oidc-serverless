"""Infrastructure the console can deploy and destroy (Terraform stacks under
terraform/stacks/<serverless|kubernetes>/<gcp|azure>/<stack>).

Only for trying the credential provider where nothing exists yet. In production the targets are
already there and only the Serverless and Kubernetes consoles' configure steps run.

Nothing about a target is written into the stacks. Each stack's inputs are the variables its
variables.tf declares; the Infrastructure page shows them as a form and saves what is filled in
as JSON (<state root>/<env>/<stack>.tfvars.json, outside git), passed with -var-file. A cloud-level
variable two stacks of the same cloud share (jfrog_url, gcp_project_id, ...) is prefilled from the last
value saved for it in that cloud; clouds never prefill from each other.

Serverless and Kubernetes are separate kinds of infrastructure: no stack of one stands on a stack
of the other, and prefill never crosses between them. The cluster stacks (gke-cluster, aks-cluster)
use their cloud's foundation only for remote state and pipeline trust (their stack-setup pipeline,
pipelines/kubernetes/<cloud>/<stack>/stack-setup.yml); nothing stands on them. Clouds are completely separate, as in production: each has its own foundation stack
(gcp-foundation, azure-foundation) with its own ADO trust, its own pipeline variable group and its
own state store.

Infrastructure never touches JFrog: no foundation, workload or cluster stack has a JFrog provider,
input or setting, and none registers anything for JFrog OIDC (its pipelines, environments and approvals
are made by the OIDC stacks themselves). The UI serves them apart (server.py scopes: /setup infra,
/oidc JFrog OIDC), as do the pipelines (terraform-stack.sh vs oidc-stack.sh). JFrog is OIDC configuration, made only once the infrastructure exists: the
"JFrog OIDC" stacks (terraform/stacks/oidc/<cloud>/<cloud>-jfrog-trust: the pipelines' JFrog
integration, mappings and JFrog variable group; on GCP also the console operator; aks-jfrog-trust: the
AKS cluster's issuer, mapping and credential provider, in its own ADO pipeline) and, per workload,
the console's workspaces. Its stacks' remote state is
-backend-config derived from that foundation's inputs only: gcs bucket <gcp_project_id>-tfstate;
azurerm account st<ado_org>tfstate in the ADO identity's resource group. Foundations keep local state.
Prefill is kept per kind and cloud: never across clouds, never between Serverless and Kubernetes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS_DIR = REPO_ROOT / "terraform" / "stacks"
STATE_ROOT = REPO_ROOT / ".console" / "setup"

# id = the stack directory. kind: Serverless or Kubernetes, independent of each other.
# Order is the order to deploy in; destroy in reverse.
# about: what the stack creates, in plain words. next: what to do once it is deployed.
CATALOGUE = (
    {"id": "gcp-foundation", "kind": "Serverless", "group": "Google Cloud", "title": "DevOps integrations",
     "about": "Lets the Google Cloud pipelines sign in to Google Cloud with their OIDC token, with no "
              "stored keys: the workload identity provider, the pipeline service accounts, their ADO "
              "service connections and variable group, the image repository, and the bucket that holds "
              "each Google Cloud workload stack's Terraform state. Nothing in JFrog.",
     "next": "Deploy the workload stacks you need, each on its own."},
    {"id": "gcp-cloud-run", "kind": "Serverless", "group": "Google Cloud", "title": "Cloud Run job",
     "about": "The job's own service account, and the Cloud Run job itself once deploy_job is on.",
     "next": "Run this stack's build-deploy pipeline to push the first image, then set job_image, turn on "
             "deploy_job (Inputs) and deploy again."},
    {"id": "gcp-cloud-run-service", "kind": "Serverless", "group": "Google Cloud", "title": "Cloud Run service",
     "about": "The service's own service account, and the Cloud Run service itself once deploy_service is on.",
     "next": "Run this stack's build-deploy pipeline to push the first image, then set service_image, turn on "
             "deploy_service (Inputs) and deploy again."},
    {"id": "gcp-cloud-run-function", "kind": "Serverless", "group": "Google Cloud", "title": "Cloud Run function",
     "about": "The function's own service account and source bucket, and the function itself once "
              "deploy_function is on.",
     "next": "Run this stack's build-deploy pipeline to upload the first source package, then set "
             "source_object, turn on deploy_function (Inputs) and deploy again."},
    {"id": "gcp-cloud-run-worker-pool", "kind": "Serverless", "group": "Google Cloud", "title": "Cloud Run worker pool",
     "about": "The worker pool's own service account, and the Cloud Run worker pool itself (always on, no "
              "HTTP endpoint) once deploy_worker_pool is on.",
     "next": "Run this stack's build-deploy pipeline to push the first image, then set worker_pool_image, "
             "turn on deploy_worker_pool (Inputs) and deploy again."},
    {"id": "azure-foundation", "kind": "Serverless", "group": "Azure", "title": "DevOps integrations",
     "about": "Lets the Azure pipelines sign in to Azure with their OIDC token, with no stored "
              "secrets: the setup identity and its federated credentials, its ADO service connections "
              "and variable group, and the storage account that holds each Azure workload stack's "
              "Terraform state. Nothing in JFrog.",
     "next": "Deploy the workload stacks you need, each on its own."},
    {"id": "azure-container-apps", "kind": "Serverless", "group": "Azure", "title": "Container Apps job",
     "about": "Its own resource group with a container registry, the job's identity, a Container Apps "
              "environment, and the job itself once deploy_job is on.",
     "next": "Run this stack's build-deploy pipeline to push the first image, then set job_image, turn on "
             "deploy_job (Inputs) and deploy again."},
    {"id": "azure-container-app", "kind": "Serverless", "group": "Azure", "title": "Container App",
     "about": "Its own resource group with a container registry, the app's identity, a Container Apps "
              "environment, and the container app itself once deploy_app is on.",
     "next": "Run this stack's build-deploy pipeline to push the first image, then set app_image, turn on "
             "deploy_app (Inputs) and deploy again."},
    {"id": "azure-functions-aca", "kind": "Serverless", "group": "Azure", "title": "Functions on Container Apps",
     "about": "Its own resource group with a container registry, the function's identity, a Container "
              "Apps environment, host storage, and the function app (kind functionapp) once "
              "deploy_function is on.",
     "next": "Run this stack's build-deploy pipeline to push the first image, then set "
             "function_image, turn on deploy_function (Inputs) and deploy again."},
    {"id": "azure-functions-premium", "kind": "Serverless", "group": "Azure", "title": "Functions Premium / Dedicated",
     "about": "Its own resource group with the function's identity, host storage, an Elastic Premium "
              "(or Dedicated) plan and the function app.",
     "next": "Run this stack's build-deploy pipeline to deploy the code."},
    {"id": "azure-functions-flex", "kind": "Serverless", "group": "Azure", "title": "Functions Flex Consumption",
     "about": "Its own resource group with the function's identity, host storage with its deployment "
              "container, a Flex Consumption plan and the function app.",
     "next": "Run this stack's build-deploy pipeline to deploy the code."},
    {"id": "azure-web-app", "kind": "Serverless", "group": "Azure", "title": "Web App",
     "about": "Its own resource group with the app's identity, an App Service plan (Linux or Windows) and "
              "the web app for Python, Java, .NET, Node.js or PHP code.",
     "next": "Run this stack's build-deploy pipeline to deploy the code."},
    # JFrog OIDC: only after the infrastructure exists. Local state, run here with the operator's
    # JFrog admin token; each stands on its cloud's foundation (it reads the service connections).
    {"id": "gcp-jfrog-trust", "kind": "JFrog OIDC", "group": "Google Cloud", "title": "Pipelines' JFrog trust",
     "after": "gcp-foundation",
     "about": "The JFrog OIDC integration for the Google Cloud pipelines' service connections, its build "
              "(reader) and setup (admin) mappings, the pipelines' JFrog variable group, and the console "
              "operator's identity.",
     "next": "Run the build-deploy pipelines, then configure each workload in the Serverless console."},
    {"id": "azure-jfrog-trust", "kind": "JFrog OIDC", "group": "Azure", "title": "Pipelines' JFrog trust",
     "after": "azure-foundation",
     "about": "The JFrog OIDC integration for the Azure pipelines' service connections, its build "
              "(reader) and setup (admin) mappings, and the pipelines' JFrog variable group.",
     "next": "Run the build-deploy pipelines, then configure each workload in the Serverless console."},
    {"id": "gke-cluster", "kind": "Kubernetes", "group": "Google Cloud", "title": "GKE cluster",
     "about": "A test GKE cluster and node pool with its OIDC issuer on.",
     "next": "Run the credentials command, then use the cluster from the Kubernetes console."},
    {"id": "aks-cluster", "kind": "Kubernetes", "group": "Azure", "title": "AKS cluster",
     "about": "A test AKS cluster, in its own resource group, with its OIDC issuer on, and the Argo CD hub "
              "(gitops/argocd/values.yaml) whose repo-server signs in with its own managed identity.",
     "next": "Run the credentials command, then use the cluster from the Kubernetes console."},
    # The AKS cluster's JFrog OIDC: after the cluster, in its own stack-setup pipeline (JFrog token from
    # the setup connection's OIDC token). Each redeployed cluster has a new issuer, trusted again here.
    {"id": "aks-jfrog-trust", "kind": "JFrog OIDC", "section": "Kubernetes", "group": "Azure",
     "title": "AKS credential provider", "after": ("aks-cluster", "azure-jfrog-trust"),
     "about": "The AKS cluster's issuer trusted in JFrog, its credential provider's service account mapped "
              "to a JFrog user, and an Argo CD Application that installs the kubelet credential provider "
              "on the labelled nodes. Nothing runs from your machine: it is an ADO pipeline.",
     "next": "Verify and prove it from the Kubernetes console."},
)
IDS = tuple(s["id"] for s in CATALOGUE)
# Only cloud-level inputs prefill another stack's form. A workload's own name, image and switches
# never do: each workload stack is separate and two must never get the same resources.
SHARED_INPUTS = frozenset({"jfrog_url", "jfrog_reader_group", "ado_org", "ado_project", "ado_tenant_id",
                           "pipeline_environment", "ado_repository", "ado_approvers", "gcp_project_id", "gcp_region",
                           "azure_subscription_id", "azure_tenant_id", "azure_location",
                           "deployer_service_account", "deployer_principal_id", "image_repository", "tags"})
# The foundation whose inputs a remote backend's location derives from, by backend type.
FOUNDATION = {"gcs": "gcp-foundation", "azurerm": "azure-foundation"}
MAX_VARS = 64 * 1024
_SECRETISH = re.compile(r"(password|secret|(^|_)(api_)?key$|(^|_)token$)", re.I)
# Inputs a picker can offer live choices for (read-only CLI listings).
OPTION_SOURCES = {"gcp_project_id": "gcp_projects", "azure_subscription_id": "azure_subscriptions",
                  "azure_tenant_id": "azure_tenants",
                  # People, by email, from live data: an unknown one would fail the pipeline.
                  "ado_approvers": "ado_users", "console_operators": "gcp_members", "azure_console_operators": "azure_users"}


class StackError(ValueError):
    pass


def _base(stack_id: str) -> dict:
    for s in CATALOGUE:
        if s["id"] == stack_id:
            return s
    raise StackError(f"unknown stack {stack_id!r}")


# More than one deployment of a Serverless workload stack: <stack>@<workload>, e.g. a second Web App
# on another OS or language. Same Terraform folder and pipelines; its own inputs file, its own state
# (key <stack>/<env>/<workload>), its own runs, plans and status. It shares nothing with the first one.
INSTANCE_NAME = re.compile(r"[a-z][a-z0-9-]{1,38}[a-z0-9]")


def multi(stack_id: str) -> bool:
    """A stack that can stand more than once: a Serverless workload (not a foundation)."""
    meta = _base(stack_id)
    return meta["kind"] == "Serverless" and not stack_id.endswith("-foundation")


def split(stack_id: str) -> tuple:
    base, _, inst = stack_id.partition("@")
    return base, inst or None


def get(stack_id: str) -> dict:
    base, inst = split(stack_id)
    meta = _base(base)
    if inst is None:
        return meta
    if not multi(base) or not INSTANCE_NAME.fullmatch(inst):
        raise StackError(f"unknown stack {stack_id!r}")
    return dict(meta, id=stack_id, base=base, instance=inst, title=f"{meta['title']} {inst}")


def instances(env: str, state_root=None) -> tuple:
    """The extra deployments with saved inputs, as <stack>@<workload>, in catalogue order."""
    d = Path(STATE_ROOT if state_root is None else state_root) / env
    found = sorted(p.name[:-len(".tfvars.json")] for p in d.glob("*@*.tfvars.json")) if d.is_dir() else []
    out = []
    for sid in found:
        try:
            get(sid)
        except StackError:
            continue
        out.append(sid)
    return tuple(sorted(out, key=lambda x: (IDS.index(split(x)[0]), x)))


def all_ids(env: str, state_root=None) -> tuple:
    """Every stack and every extra deployment, each right after its stack."""
    extra = instances(env, state_root)
    return tuple(x for sid in IDS for x in (sid, *(i for i in extra if split(i)[0] == sid)))


def check_name(stack_id: str, workload, env: str, state_root=None) -> str:
    """A new deployment's name, refused when malformed or already used by any deployment of any
    workload stack here (the console's own record; the cloud is asked separately)."""
    state_root = STATE_ROOT if state_root is None else state_root
    workload = str(workload or "").strip().lower()
    if not INSTANCE_NAME.fullmatch(workload):
        raise StackError("3 to 40 lowercase letters, digits or hyphens, starting with a letter")
    for x in all_ids(env, state_root):
        if not multi(split(x)[0]):
            continue
        if workload in (split(x)[1], str(read_inputs(x, env, state_root).get("workload") or "")):
            raise StackError(f"{workload} is already used by {get(x)['title']}: pick another name")
    return workload


def new_instance(stack_id: str, workload: str, env: str, root=None, state_root=None) -> str:
    """Start another deployment of a workload stack, named by its workload: its inputs start from the
    stack's own saved inputs with the new name. Refuses a name any deployment of it already uses."""
    root = STACKS_DIR if root is None else root
    state_root = STATE_ROOT if state_root is None else state_root
    if "@" in stack_id or not multi(stack_id):
        raise StackError(f"{stack_id} cannot be deployed more than once")
    workload = check_name(stack_id, workload, env, state_root)
    sid = f"{stack_id}@{workload}"
    start = {k: v for k, v in read_inputs(stack_id, env, state_root).items() if k != "slot"}
    _write(inputs_file(sid, env, state_root), dict(start, workload=workload))
    return sid


CLOUD_DIR = {"Google Cloud": "gcp", "Azure": "azure"}
KIND_DIR = {"Serverless": "serverless", "Kubernetes": "kubernetes", "JFrog OIDC": "oidc"}


def rel_dir(stack_id: str) -> str:
    """Where a stack lives under terraform/stacks: <kind>/<cloud>/<id>. Serverless and Kubernetes,
    and each cloud, have their own tree; the id (state key, ADO environment) never changes."""
    meta = get(stack_id)
    return f"{KIND_DIR[meta['kind']]}/{CLOUD_DIR[meta['group']]}/{meta.get('base', stack_id)}"


def stack_dir(stack_id: str, root: Path = STACKS_DIR) -> Path:
    return Path(root) / rel_dir(stack_id)


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


def _choices(name: str, body: str) -> list | None:
    """The allowed values of a `contains([...], var.<name>)` validation: the form offers them as a picker."""
    m = re.search(r"contains\(\s*(\[[^\]]*\])\s*,\s*var\." + re.escape(name) + r"\s*\)", body)
    if not m:
        return None
    try:
        vals = json.loads(m.group(1))
    except ValueError:
        return None
    return vals if vals and all(isinstance(x, str) for x in vals) else None


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


def _shape(t: str) -> dict:
    """How the form edits a type: string, number, bool, list (of text), map (text to text) or object
    (named text fields). Anything else is "json", which the form cannot edit."""
    t = re.sub(r"\s+", " ", t).strip()
    if t in ("string", "number", "bool"):
        return {"kind": t}
    if t in ("list(string)", "set(string)"):
        return {"kind": "list"}
    if t == "map(string)":
        return {"kind": "map"}
    m = re.fullmatch(r"object\(\{(.*)\}\)", t)
    if m:
        pairs = re.findall(r"(\w+)\s*=\s*(\w+)", m.group(1))
        if pairs and all(ty == "string" for _, ty in pairs):
            return {"kind": "object", "fields": [n for n, _ in pairs]}
    return {"kind": "json"}


def variables(stack_id: str, root: Path = STACKS_DIR) -> list:
    """The stack's declared inputs: name, kind (see _shape), required, default, description."""
    out = []
    for f in sorted(stack_dir(stack_id, root).glob("*.tf")):
        for name, body in _blocks(f.read_text()):
            a = _attrs(body)
            t = a.get("type", "any").strip()
            desc = a.get("description", "").strip()
            v = {"name": name, **_shape(t), "type": t,
                 "required": "default" not in a,
                 "description": desc[1:-1] if desc.startswith('"') and desc.endswith('"') else desc,
                 "sensitive": a.get("sensitive", "").strip() == "true" or bool(_SECRETISH.search(name))}
            if "default" in a:
                ok, val = _literal(a["default"])
                if ok:
                    v["default"] = val
            if name in OPTION_SOURCES:
                v["options"] = OPTION_SOURCES[name]
            choices = _choices(name, body)
            if choices:
                v["choices"] = choices
            out.append(v)
    return out


def backend_kind(stack_id: str, root: Path = STACKS_DIR) -> str | None:
    p = stack_dir(stack_id, root) / "backend.tf"
    m = re.search(r'backend\s+"([a-z0-9]+)"', p.read_text()) if p.is_file() else None
    return m.group(1) if m else None


def _after(stack_id: str) -> tuple:
    a = get(stack_id).get("after") or ()
    return (a,) if isinstance(a, str) else tuple(a)


def depends_on(stack_id: str, root: Path = STACKS_DIR) -> str | None:
    """The first stack this one stands on: its `after`, else the foundation whose store holds its state
    (a JFrog OIDC stack reads its service connections). None for a foundation (local state) itself."""
    ups = upstreams(stack_id, root)
    return ups[0] if ups else None


def upstreams(stack_id: str, root: Path = STACKS_DIR) -> tuple:
    """Every stack this one stands on directly: its `after` stacks (whose outputs, pipeline or trust it
    uses) and the foundation whose store holds its remote state."""
    kind = backend_kind(stack_id, root)
    store = FOUNDATION.get(kind) if kind else None
    ups = [*_after(stack_id), *([store] if store and store != stack_id else [])]
    return tuple(dict.fromkeys(ups))


def ancestors(stack_id: str, root: Path = STACKS_DIR) -> tuple:
    """Everything a stack stands on, directly or through another stack, in deploy (catalogue) order.
    None of it may be missing when the stack is deployed."""
    seen, todo = set(), list(upstreams(stack_id, root))
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo.extend(upstreams(x, root))
    return tuple(x for x in IDS if x in seen)


def descendants(stack_id: str, root: Path = STACKS_DIR) -> tuple:
    """Everything that stands on a stack, directly or through another stack, in destroy (reverse
    catalogue) order. None of it may still stand when the stack is destroyed."""
    return tuple(x for x in reversed(all_ids(_ENV[0], _ENV[1])) if stack_id in ancestors(x, root))


# The environment and state root whose extra deployments descendants() sees (set by the console).
_ENV = ["test", None]


def use_env(env: str, state_root=None) -> None:
    _ENV[0], _ENV[1] = env, state_root


# A workload's build-deploy pipeline reads its cloud's JFrog variable group (JFROG_URL,
# JFROG_OIDC_PROVIDER), which only that cloud's JFrog trust stack writes.
TRUST = {"Google Cloud": "gcp-jfrog-trust", "Azure": "azure-jfrog-trust"}


def build_upstreams(stack_id: str, root: Path = STACKS_DIR) -> tuple:
    """What a workload's build-deploy run needs standing: the stack itself, all it stands on, and its
    cloud's JFrog trust (with all that stands under it). Deploy order."""
    trust = TRUST.get(get(stack_id)["group"])
    need = {stack_id, *ancestors(stack_id, root), *((trust, *ancestors(trust, root)) if trust else ())}
    return tuple(x for x in IDS if x in need)


# -- saved inputs ------------------------------------------------------------------------------------

def inputs_file(stack_id: str, env: str, state_root: Path = STATE_ROOT) -> Path:
    get(stack_id)
    return Path(state_root) / env / f"{stack_id}.tfvars.json"


def _shared_file(env: str, state_root: Path, stack_id: str) -> Path:
    """Prefill values are kept per kind and cloud: never across clouds or between kinds."""
    meta = get(stack_id)
    group = re.sub(r"[^a-z0-9]+", "-", f"{meta['kind']} {meta['group']}".lower()).strip("-")
    return Path(state_root) / env / f"shared-{group}.json"


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
    saved, shared = read_inputs(stack_id, env, state_root), _load(_shared_file(env, state_root, stack_id))
    after = get(stack_id).get("after")
    if after:   # a JFrog OIDC stack prefills from its own cloud's foundation, never another cloud's
        shared = {**read_inputs(after, env, state_root), **shared}
    fields = []
    for v in variables(stack_id, root):
        if v["sensitive"]:
            continue   # credentials come from the environment at run time, never from the form
        f = dict(v)
        if v["name"] in saved:
            f["current"], f["source"] = saved[v["name"]], "saved"
        elif v["name"] in shared and v["name"] in SHARED_INPUTS:
            f["current"], f["source"] = shared[v["name"]], "shared"
        elif "default" in v:
            f["current"], f["source"] = v["default"], "default"
        if name_locked := (v["name"] == "workload" and get(stack_id).get("instance")):
            f["current"], f["locked"] = get(stack_id)["instance"], name_locked
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
    text = lambda x: isinstance(x, str)
    if k == "list":
        if not isinstance(raw, list) or not all(map(text, raw)):
            raise StackError(f"{v['name']}: a list of text values expected")
        return [x.strip() for x in raw if x.strip()]
    if k == "map":
        if not isinstance(raw, dict) or not all(text(a) and text(b) for a, b in raw.items()):
            raise StackError(f"{v['name']}: names with text values expected")
        return {a.strip(): b.strip() for a, b in raw.items() if a.strip()}
    if k == "object":
        if not isinstance(raw, dict) or set(raw) != set(v["fields"]) or not all(text(x) and x.strip() for x in raw.values()):
            raise StackError(f"{v['name']}: {', '.join(v['fields'])} expected")
        return {a: raw[a].strip() for a in v["fields"]}
    return raw


def typed_inputs(stack_id: str, raw: dict, root: Path = STACKS_DIR) -> dict:
    """A pipeline's inputs with their declared types back: ADO passes an object parameter's
    scalars as text ("False", "1"), which Terraform refuses for bool and number variables."""
    kinds = {v["name"]: v["kind"] for v in variables(stack_id, root)}
    out = {}
    for k, val in raw.items():
        kind = kinds.get(k)
        if kind == "bool" and isinstance(val, str):
            if val.lower() not in ("true", "false"):
                raise StackError(f"{k}: true or false expected")
            val = val.lower() == "true"
        elif kind == "number" and isinstance(val, str):
            val = _coerce({"name": k, "kind": "number"}, val)
        out[k] = val
    return out


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
        if v.get("choices") and out[name] not in v["choices"]:
            raise StackError(f"{name} must be one of: {', '.join(v['choices'])}")
    meta = get(stack_id)
    if meta.get("instance"):   # a deployment's name is fixed: another name is another deployment
        out["workload"] = meta["instance"]
    _write(inputs_file(stack_id, env, state_root), out)
    others = {x["name"] for s in CATALOGUE if s["id"] != meta.get("base", stack_id) and (s["kind"], s["group"]) == (meta["kind"], meta["group"])
              for x in variables(s["id"], root)}
    shared = _load(_shared_file(env, state_root, stack_id))
    shared.update({k: val for k, val in out.items() if k in others and k in SHARED_INPUTS})
    _write(_shared_file(env, state_root, stack_id), shared)
    return out


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


def separate_jfrog_inputs(env: str, root: Path = STACKS_DIR, state_root: Path = STATE_ROOT) -> None:
    """Infrastructure inputs hold nothing for JFrog. Values a foundation no longer declares but its
    cloud's JFrog OIDC stack does (inputs saved before the split) move there, without overwriting
    what that stack already has; the infrastructure prefill drops every name no stack of its own
    kind and cloud declares. Idempotent; run when Setup starts."""
    declared = {s["id"]: {v["name"] for v in variables(s["id"], root)} for s in CATALOGUE if stack_dir(s["id"], root).is_dir()}
    for s in CATALOGUE:
        dep = s.get("after")
        if s["kind"] != "JFrog OIDC" or dep not in declared or s["id"] not in declared:
            continue
        saved = read_inputs(dep, env, state_root)
        extra = {k: v for k, v in saved.items() if k not in declared[dep]}
        if not extra:
            continue
        moved = {k: v for k, v in extra.items() if k in declared[s["id"]]}
        if moved:
            mine = read_inputs(s["id"], env, state_root)
            _write(inputs_file(s["id"], env, state_root), {**moved, **mine})
        _write(inputs_file(dep, env, state_root), {k: v for k, v in saved.items() if k in declared[dep]})
    by_file = {}
    for s in CATALOGUE:
        if s["kind"] != "JFrog OIDC" and s["id"] in declared:
            by_file.setdefault(_shared_file(env, state_root, s["id"]), set()).update(declared[s["id"]])
    for f, names in by_file.items():
        shared = _load(f)
        if any(k not in names for k in shared):
            _write(f, {k: v for k, v in shared.items() if k in names})


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
    """-backend-config pairs for a gcs or azurerm backend, matching the stores ADO trust creates. An
    extra deployment (<stack>@<workload>, or `instance` in `found` from a pipeline) keeps its own state."""
    base, inst = split(stack_id)
    inst = inst or found.get("instance") or None
    if inst and (not multi(base) or not INSTANCE_NAME.fullmatch(str(inst))):
        raise StackError(f"{stack_id}: {inst!r} is not a deployment name")
    stack_id, where = base, f"{base}/{env}/{inst}" if inst else f"{base}/{env}"
    need = {"gcs": ("gcp_project_id",), "azurerm": ("azure_subscription_id", "ado_org", "state_resource_group")}.get(kind)
    if need is None:
        raise StackError(f"{stack_id}: backend {kind!r} is not supported")
    gaps = [k for k in need if not found.get(k)]
    if gaps:
        raise StackError(f"save the {FOUNDATION[kind]} inputs first ({', '.join(gaps)}): its state store holds this stack's state")
    if kind == "gcs":
        pairs = {"bucket": f"{found['gcp_project_id']}-tfstate", "prefix": where}
    else:
        pairs = {"subscription_id": found["azure_subscription_id"], "resource_group_name": found["state_resource_group"],
                 "storage_account_name": ("st" + found["ado_org"].replace("-", "") + "tfstate")[:24],
                 "container_name": "tfstate", "key": f"{where}.tfstate", "use_azuread_auth": "true"}
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
    prints one -backend-config=k=v per line (nothing for a local-state stack);
    python3 -m setup_console.stacks dir <stack> prints its directory under terraform/stacks;
    python3 -m setup_console.stacks typed-inputs <stack> <inputs.json> rewrites the file with declared types."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "dir":   # the stack's directory, relative to terraform/stacks
        print(rel_dir(argv[1]))
        return 0
    if len(argv) == 3 and argv[0] == "typed-inputs":
        p = Path(argv[2])
        p.write_text(json.dumps(typed_inputs(argv[1], json.loads(p.read_text() or "{}"))))
        return 0
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
