"""The plugin's own log: /var/log/jfrog-credentials-provider/jfrog-credentials-provider.log.

JSON lines on the node. CHECK_KUBELET_PROVIDER prints the directory listing,
the last 15 lines and the latest `RT oidc token url` lines. Plain-text lines
(and ADO timestamp prefixes) are accepted too, so a pasted log still parses.
Each entry gets a `kind` and the runbook's `meaning`.
"""

from __future__ import annotations

import json
import re

from .. import logic

ADO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s")

KINDS = [
    ("envs", re.compile(r"getting envs\s*-\s*azureAppAudience:\s*(?P<audience>[^,]*),\s*jfrogOidcProviderName:\s*(?P<provider>[^\s,\"]*)"),
     "The values the plugin actually received from the node config. Compare with the values file"),
    ("sa_token", re.compile(r"Service Account Token obtained using Pod Identity"),
     "kubelet passed a projected ServiceAccount token (the Option B path is active)"),
    ("token_url", re.compile(r"RT oidc token url\s*:\s*(?P<url>\S+)"),
     "The URL it will call. It must contain exactly one https://"),
    ("dns_error", re.compile(r"createArtifactoryToken.*dial tcp: lookup .*no such host|dial tcp: lookup \S+.*no such host"),
     "Name resolution failed: look at the URL on the previous line"),
    ("autoupdate", re.compile(r"Auto-update functionality is disabled"),
     "Expected; disable_provider_autoupdate is \"true\""),
    ("token_http_error", re.compile(r"(?:status|code)\D{0,12}(?P<status>40[13])\b.*(?:oidc|token)|(?:oidc|token).*(?:status|code)\D{0,12}(?P<status2>40[13])\b", re.I),
     "Artifactory refused the exchange: OIDC provider, issuer or identity-mapping mismatch (expected, not observed)"),
    ("readonly_fs", re.compile(r"read-only file system"),
     "Diagnostic artifact, not a broken binary: the diagnostic pod mounts the host read-only"),
]


def _strip(line: str) -> str:
    return ADO_PREFIX.sub("", line.rstrip("\r\n"))


def parse_line(line: str):
    raw = _strip(line)
    if not raw.strip():
        return None
    entry = {"raw": raw, "time": "", "level": "", "msg": raw.strip()}
    s = raw.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            entry["msg"] = str(obj.get("msg") or obj.get("message") or obj.get("Msg") or "")
            entry["time"] = logic.normalize_ts(obj.get("time") or obj.get("ts") or obj.get("timestamp") or "")
            entry["level"] = str(obj.get("level") or obj.get("lvl") or "").lower()
            for extra in ("error", "err", "cause"):
                if obj.get(extra):
                    entry["msg"] += f" {extra}={obj[extra]}"
    else:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*)\s+(?:(\w+)\s+)?(.*)$", s)
        if m:
            entry["time"] = logic.normalize_ts(m.group(1))
            entry["level"] = (m.group(2) or "").lower()
            entry["msg"] = m.group(3)
    if "ERROR" in entry["msg"] and not entry["level"]:
        entry["level"] = "error"
    entry["kind"], entry["meaning"] = "other", ""
    for kind, rx, meaning in KINDS:
        m = rx.search(entry["msg"])
        if m:
            entry["kind"], entry["meaning"] = kind, meaning
            if kind == "token_url":
                entry["url"] = m.group("url")
                entry["schemes"] = logic.scheme_count(m.group("url"))
            elif kind == "envs":
                entry["audience"] = m.group("audience").strip()
                entry["provider"] = m.group("provider").strip()
            break
    return entry


def parse(text: str) -> list:
    return [e for e in (parse_line(l) for l in (text or "").splitlines()) if e]


def newest(entries: list, kind: str):
    """The newest entry of a kind, by timestamp (older lines may still show the
    doubled URL after a fix, so always go by the newest)."""
    found = [e for e in entries if e.get("kind") == kind]
    if not found:
        return None
    timed = [e for e in found if e.get("time")]
    return max(timed, key=lambda e: e["time"]) if timed else found[-1]


def summary(entries: list) -> dict:
    url = newest(entries, "token_url")
    envs = newest(entries, "envs")
    return {
        "newest_token_url": url["url"] if url else None,
        "newest_token_url_time": url["time"] if url else None,
        "newest_schemes": url["schemes"] if url else None,
        "doubled_seen": any(e.get("kind") == "token_url" and e["schemes"] > 1 for e in entries),
        "dns_errors": sum(1 for e in entries if e.get("kind") == "dns_error"),
        "sa_token_seen": any(e.get("kind") == "sa_token" for e in entries),
        "envs": {"azure_app_audience": envs["audience"], "provider_name": envs["provider"]} if envs else None,
        "token_http_errors": sum(1 for e in entries if e.get("kind") == "token_http_error"),
        "readonly_fs": any(e.get("kind") == "readonly_fs" for e in entries),
        "lines": len(entries),
    }
