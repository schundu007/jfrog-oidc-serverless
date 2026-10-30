"""Decision logic shared by the real checks and the simulator (spec §8).

Everything here is pure: text and dicts in, text and dicts out. The simulator
calls the same functions the checks call, so a scenario exercises the
console's own code rather than a copy of it.

Upstream facts encoded here (read from jfrog/jfrog-credentials-provider `main`
on 2026-10-16; the deployed chart is 1.4.0, so re-check against that tag):
- internal/handlers/jfrog.go builds the token URL as
  "https://" + artifactory_url + "/access/api/v1/oidc/token".
- internal/utils/utils.go MergeFiles replaces an existing entry only when its
  artifactory_url equals the new one.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re

JFROG_PROVIDER = "jfrog-credentials-provider"
ACR_PROVIDER = "acr-credential-provider"
TOKEN_PATH = "/access/api/v1/oidc/token"
NODE_CONFIG = "/var/lib/kubelet/credential-provider-config.yaml"
NODE_BIN_DIR = "/var/lib/kubelet/credential-provider"
PLUGIN_LOG = "/var/log/jfrog-credentials-provider/jfrog-credentials-provider.log"
BACKUP_SUFFIX = ".bak-before-jfrog-reset"
EXPECTED_BINARY_BYTES = 14_359_603          # jfrog-credential-provider 1.4.0 linux, as mirrored in the pilot
MIN_BINARY_BYTES = 1_000_000                # the mirror step's size guard
APPROVED_CHART_REPO = "https://charts.jfrog.io"
APPROVED_CHART_NAME = "jfrog/jfrog-credential-provider"
ARCH_SUFFIXES = ("-amd64", "-arm64", "-x86_64", "-aarch64")

_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::\d{1,5})?$", re.I)


# ------------------------------------------------------------------ URL building

def token_url(artifactory_url: str) -> str:
    """Exactly what the plugin does. A scheme in artifactory_url doubles."""
    return "https://" + (artifactory_url or "") + TOKEN_PATH


def scheme_count(url: str) -> int:
    """How many `https:` a token URL carries. Go's error line prints the
    doubled form as https://https//host, so the colon is optional."""
    return len(re.findall(r"https?:?//", url or ""))


def is_bare_host(value: str) -> bool:
    return bool(value) and "://" not in value and "/" not in value and bool(_HOST.match(value))


def bare_host_error(value: str):
    """None if value is a bare hostname, else why not (the runbook's wording)."""
    if not value:
        return "required"
    if value.lower().startswith(("https://", "http://")):
        return "must be a bare hostname: `https://` here produces `https://https://...` in the plugin's token URL"
    if "/" in value:
        return "must be a bare hostname: no path"
    if not _HOST.match(value):
        return "is not a valid hostname"
    return None


# ------------------------------------------------------------------ matchImages

def image_host(image: str) -> str:
    """Registry host of an image reference (docker.io when none is given)."""
    first = (image or "").split("/", 1)[0]
    if "/" in (image or "") and ("." in first or ":" in first or first == "localhost"):
        return first.lower()
    return "docker.io"


def _split_ref(ref: str):
    ref = re.sub(r"^[a-z]+://", "", ref or "")
    host, _, path = ref.partition("/")
    host, _, port = host.partition(":")
    return host.lower(), port, path


def match_image(pattern: str, image: str) -> bool:
    """kubelet's matchImages rule: same number of host labels, each matched as a
    glob (so `*.azurecr.io` matches `x.azurecr.io`, not `a.b.azurecr.io`), equal
    port, and the pattern's path (if any) is a prefix of the image path."""
    p_host, p_port, p_path = _split_ref(pattern)
    i_host, i_port, i_path = _split_ref((image or "").split("@", 1)[0])
    if ":" in i_path.rsplit("/", 1)[-1]:
        i_path = i_path.rsplit(":", 1)[0]
    p_parts, i_parts = p_host.split("."), i_host.split(".")
    if len(p_parts) != len(i_parts):
        return False
    if not all(fnmatch.fnmatchcase(i, p) for p, i in zip(p_parts, i_parts)):
        return False
    if p_port != i_port:
        return False
    return not p_path or i_path == p_path or i_path.startswith(p_path.rstrip("/") + "/")


def providers_for_image(providers: list, image: str) -> list:
    return [p.get("name") for p in providers or [] if any(match_image(m, image) for m in p.get("matchImages") or [])]


def dedicated_host(platform_host: str, repo_key: str) -> str:
    """`example.jfrog.io` + `aks-sandbox-test-docker-local` → `example-aks-sandbox-test-docker-local.jfrog.io`."""
    server, _, domain = (platform_host or "").partition(".")
    return f"{server}-{repo_key}.{domain}" if domain and repo_key else ""


# ------------------------------------------------------------------ MergeFiles

def merge_files(providers: list, new_entry: dict, *, observed: bool = True):
    """The DaemonSet setup step merging the release's entry into the node file.

    Returns (providers, action). An entry with the same artifactory_url is
    replaced ("replaced"); no JFrog entry yet means it is added ("added").
    A JFrog entry with a different artifactory_url is a different entry to
    MergeFiles. The upstream source suggests the new one would be appended, but
    on sandbox the node kept the old entry alone, and why no second entry appeared
    was never established. observed=True reproduces what was seen
    ("kept-stale"); observed=False follows the source reading ("appended").
    """
    out = [dict(p) for p in providers or []]
    url = entry_url(new_entry)
    for i, p in enumerate(out):
        if p.get("name") == JFROG_PROVIDER and entry_url(p) == url:
            out[i] = dict(new_entry)
            return out, "replaced"
    if not any(p.get("name") == JFROG_PROVIDER for p in out):
        return out + [dict(new_entry)], "added"
    if observed:
        return out, "kept-stale"
    return out + [dict(new_entry)], "appended"


def entry_env(entry: dict) -> dict:
    env = (entry or {}).get("env") or []
    if isinstance(env, dict):
        return dict(env)
    return {e.get("name"): e.get("value") for e in env if isinstance(e, dict) and e.get("name")}


def entry_url(entry: dict):
    return entry_env(entry).get("artifactory_url")


# ------------------------------------------------------------------ RESET_NODE_JFROG_ENTRY

def reset_plan(text: str, *, fixed: bool = True) -> dict:
    """What RESET_NODE_JFROG_ENTRY does to one node's credential-provider-config.yaml.

    fixed=True is the current script: skip a node whose JFrog artifactory_url
    values have no `https://`; abort if a *provider-level* entry (a `- name:` at
    the JFrog entry's own indentation) follows the JFrog entry, so
    acr-credential-provider is never deleted; otherwise back up the file and
    cut the JFrog entry. fixed=False is the first version: no skip, and the
    safety check counted every `- name:` line, including the JFrog entry's own
    env: items, so it aborted on every node.
    """
    lines = (text or "").splitlines()
    start, indent = None, None
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-\s+name:\s*[\"']?" + re.escape(JFROG_PROVIDER) + r"[\"']?\s*$", line)
        if m:
            start, indent = i, len(m.group(1))
            break
    if start is None:
        return {"action": "skip", "reason": "No JFrog provider entry on this node", "text": text}
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s.strip() and (len(s) - len(s.lstrip(" "))) < indent:
            end = j            # dedent: the providers list ended
            break
    urls = [re.sub(r"^\s*value:\s*", "", lines[k + 1]).strip().strip("\"'")
            for k in range(start, end - 1) if re.match(r"^\s*-?\s*name:\s*artifactory_url\s*$", lines[k])]
    if fixed and not any(u.startswith("https://") for u in urls):
        return {"action": "skip", "reason": "Nothing to fix on this node", "urls": urls, "text": text}
    if fixed:
        following = [lines[j] for j in range(start + 1, end)
                     if re.match(r"^\s*-\s+name:", lines[j]) and len(lines[j]) - len(lines[j].lstrip(" ")) == indent]
    else:
        following = [lines[j] for j in range(start + 1, len(lines)) if re.match(r"^\s*-\s+name:", lines[j])]
    if following:
        return {"action": "abort", "reason": "another provider entry follows the JFrog entry; refusing to cut it",
                "following": [f.strip() for f in following], "urls": urls, "text": text}
    new_lines = lines[:start] + lines[end:]
    return {"action": "reset", "reason": "backed up and removed the JFrog entry", "urls": urls,
            "backup": NODE_CONFIG + BACKUP_SUFFIX, "text": "\n".join(new_lines) + ("\n" if new_lines else "")}


# ------------------------------------------------------------------ durations, caches, estimates

def duration_seconds(value):
    """Go-style durations: 5h, 30m, 1h30m, 90s, 300ms (rounded down to seconds)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    total, pos = 0.0, 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)(ms|h|m|s)", s):
        if m.start() != pos:
            return None
        total += {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[m.group(2)] * float(m.group(1))
        pos = m.end()
    return int(total) if pos and pos == len(s) else None


def human_duration(seconds) -> str:
    if seconds is None:
        return "?"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return "".join(f"{v}{u}" for v, u in ((h, "h"), (m, "m"), (s, "s")) if v) or "0s"


def normalize_ts(value) -> str:
    """Normalise the timestamps the pipeline prints to sortable UTC ISO strings."""
    if not value:
        return ""
    s = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|UTC|\+00:?00)?", s)
    return f"{m.group(1)}T{m.group(2)}Z" if m else s


def kubelet_older_than_config(kubelet_started, config_mtime):
    """True when kubelet started before the config file last changed: it is
    still running the config it read at its own start. None when unknown."""
    a, b = normalize_ts(kubelet_started), normalize_ts(config_mtime)
    if not a or not b:
        return None
    return a < b


def exchanges_per_hour(nodes: int, service_accounts: int, registries: int, cache_seconds: int) -> float:
    """Upper bound: one exchange per node × SA × registry per cache window."""
    if not cache_seconds:
        return 0.0
    return nodes * service_accounts * registries * (3600.0 / cache_seconds)


# ------------------------------------------------------------------ registry / Artifactory responses

def parse_realm(www_authenticate: str) -> dict:
    return dict(re.findall(r'(\w+)="([^"]*)"', www_authenticate or ""))


def realm_names_repo(www_authenticate: str, repo_key: str) -> bool:
    realm = parse_realm(www_authenticate).get("realm", "")
    return bool(repo_key) and f"/api/docker/{repo_key}/" in realm


def is_dormant_redirect(status: int, location: str) -> bool:
    return status in (301, 302, 303, 307) and "reactivate-server" in (location or "")


def download_url_problem(url: str):
    if not url:
        return "required"
    if not url.startswith("https://"):
        return "must be an https URL"
    for suf in ARCH_SUFFIXES:
        if url.endswith(suf):
            return f"ends in `{suf}`: the chart appends its own arch suffix, so the path doubles (curl 22 / 401)"
    return None


def binary_size_problem(size):
    if size is None:
        return "unknown size"
    if size < MIN_BINARY_BYTES:
        return f"{size} bytes is under 1 MB: likely an empty redirect body saved by curl without -L"
    if abs(size - EXPECTED_BINARY_BYTES) > EXPECTED_BINARY_BYTES * 0.2:
        return f"{size:,} bytes is far from the expected {EXPECTED_BINARY_BYTES:,} for 1.4.0"
    return None


# ------------------------------------------------------------------ approvals

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(data) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def request_hash(pipeline: str, branch: str, parameters: dict, values_sha256: str = "") -> str:
    return sha256_hex(canonical({"pipeline": pipeline, "branch": branch,
                                 "parameters": {k: str(v) for k, v in (parameters or {}).items()},
                                 "values_sha256": values_sha256 or ""}))


def same_node(a: str, b: str) -> bool:
    """Node names compare case-insensitively: kubelet logs show vmss00000G for vmss00000g."""
    return (a or "").lower() == (b or "").lower()
