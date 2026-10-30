"""Pipeline scenario output: the QUICK SUMMARY footer first, the full log as fallback.

Output used to be cut off when pasted into a tool with a ~50,000-character
limit, losing the end and the summary blocks. Every diagnostic scenario
therefore filters journal noise, windows the journal, caps the plugin-log grep
at the last 10 lines and repeats its key results in a QUICK SUMMARY footer.

Footer layout (one block per inspected node; field names from the runbook,
layout reconstructed: fix it with the first recorded live run):

    ================ QUICK SUMMARY ================
    Node inspected: aks-nodepool1-12345678-vmss00000g
    Node chosen because: node of pod jfrog/secretless-pull-test
    Kubelet flags: --image-credential-provider-config=... --image-credential-provider-bin-dir=...
    Kubelet started at: 2026-10-16T14:02:41Z
    Config file mtime: 2026-10-16T14:02:40Z
    Providers in config: acr-credential-provider, jfrog-credentials-provider
    Bin dir: acr-credential-provider, jfrog-credentials-provider (14359603 bytes)
    -- jfrog provider entry --
      - name: jfrog-credentials-provider
        ...
    -- latest OIDC token URLs --
    {"level":"info","time":"...","msg":"RT oidc token url :https://example.jfrog.io/access/api/v1/oidc/token"}
    ===============================================

Other sections in full logs are `===== title =====` banners (CHECK_JFROG_POD_CONFIG
prints `helm get values -a`, the rendered artifactory_url, the chart and the
service account).
"""

from __future__ import annotations

import re

from .. import logic
from . import kubelet_config, plugin_log

ADO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s")
BANNER = re.compile(r"^\s*={3,}\s*(?P<title>[^=]+?)\s*={3,}\s*$")
SUBSECTION = re.compile(r"^\s*-{2,}\s*(?P<title>[^-].*?)\s*-{2,}\s*$")
KV = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z0-9 _/()-]{1,40}?)\s*:\s(?P<v>.*)$")
SUMMARY_MARK = "QUICK SUMMARY"
TRUNCATION_LIMIT = 50_000


def clean(text: str) -> list:
    return [ADO_PREFIX.sub("", l.rstrip("\r\n")) for l in (text or "").splitlines()]


def sections(text: str) -> dict:
    """`===== title =====` banners → {title: body}. Later banners with the same title win."""
    out, title, body = {}, None, []
    for line in clean(text):
        m = BANNER.match(line)
        if m:
            if title is not None:
                out[title] = "\n".join(body).strip("\n")
            title, body = m.group("title").strip(), []
        elif title is not None:
            body.append(line)
    if title is not None:
        out[title] = "\n".join(body).strip("\n")
    return out


def _node_block(lines: list) -> dict:
    node = {"fields": {}, "jfrog_entry_text": "", "token_url_lines": [], "providers": [], "bin_dir": ""}
    sub, buf = None, {}
    for line in lines:
        m = SUBSECTION.match(line)
        if m:
            sub = m.group("title").lower()
            buf.setdefault(sub, [])
            continue
        if sub:
            buf[sub].append(line)
            continue
        kv = KV.match(line)
        if kv:
            node["fields"][kv.group("k").strip().lower()] = kv.group("v").strip()
    f = node["fields"]
    node["node"] = f.get("node inspected")
    node["reason"] = f.get("node chosen because") or f.get("reason")
    node["kubelet_started"] = logic.normalize_ts(f.get("kubelet started at"))
    node["config_mtime"] = logic.normalize_ts(f.get("config file mtime"))
    node["kubelet_flags"] = f.get("kubelet flags", "")
    node["providers"] = [p.strip() for p in f.get("providers in config", "").split(",") if p.strip()]
    node["bin_dir"] = f.get("bin dir", "")
    m = re.search(r"jfrog-credentials-provider\s*\((\d+) bytes\)", node["bin_dir"])
    node["binary_bytes"] = int(m.group(1)) if m else None
    for title, body in buf.items():
        if "jfrog provider entry" in title:
            node["jfrog_entry_text"] = "\n".join(body)
        elif "token url" in title:
            node["token_url_lines"] = [l for l in body if l.strip()]
    node["entry"] = _entry_from_text(node["jfrog_entry_text"])
    node["plugin"] = plugin_log.summary(plugin_log.parse("\n".join(node["token_url_lines"])))
    node["kubelet_older_than_config"] = logic.kubelet_older_than_config(node["kubelet_started"], node["config_mtime"])
    node["flags_ok"] = ("--image-credential-provider-config" in node["kubelet_flags"]
                        and "--image-credential-provider-bin-dir" in node["kubelet_flags"])
    return node


def _entry_from_text(text: str) -> dict:
    if not text.strip():
        return {}
    body = "providers:\n" + "\n".join("  " + l if not l.startswith("  ") else l for l in text.splitlines())
    parsed = kubelet_config.parse(body)
    summ = kubelet_config.jfrog_summary(parsed)
    summ["entries"] = parsed["jfrog_entries"]
    return summ


def quick_summary(text: str) -> dict:
    """Parse the footer; fall back to the full log when it is missing."""
    lines = clean(text)
    idx = max((i for i, l in enumerate(lines) if SUMMARY_MARK in l), default=None)
    source = "summary"
    if idx is None:
        source, body = "full-log", lines
    else:
        body = []
        for l in lines[idx + 1:]:
            if re.match(r"^\s*={10,}\s*$", l):
                break
            body.append(l)
    blocks, cur = [], None
    for l in body:
        if re.match(r"^\s*Node inspected\s*:", l):
            if cur is not None:
                blocks.append(cur)
            cur = [l]
        elif cur is not None:
            cur.append(l)
    if cur is not None:
        blocks.append(cur)
    nodes = [_node_block(b) for b in blocks]
    if source == "full-log" and not nodes:
        # no per-node fields at all: still pull the plugin URL lines out of the log
        pl = plugin_log.summary(plugin_log.parse("\n".join(l for l in lines if "RT oidc token url" in l)))
        if pl["lines"]:
            nodes = [{"node": None, "reason": None, "plugin": pl, "entry": {}, "fields": {},
                      "kubelet_started": "", "config_mtime": "", "kubelet_older_than_config": None,
                      "providers": [], "binary_bytes": None, "flags_ok": None, "token_url_lines": []}]
    footer = {k: v for k, v in (KV.match(l).groups() for l in body if KV.match(l))} if source == "summary" else {}
    return {"source": source, "nodes": nodes, "footer": {k.strip().lower(): v.strip() for k, v in footer.items()},
            "truncated_risk": len(text or "") > TRUNCATION_LIMIT, "chars": len(text or "")}


def pod_config_sections(text: str) -> dict:
    """CHECK_JFROG_POD_CONFIG: helm values, the rendered artifactory_url, chart and SA metadata."""
    sec = {k.lower(): v for k, v in sections(text).items()}
    find = lambda *keys: next((v for k, v in sec.items() if all(x in k for x in keys)), "")
    sa = find("service account") or find("serviceaccount")
    ann = re.search(r"JFrogExchange:\s*[\"']?(\w+)", sa)
    return {
        "helm_values": find("helm get values"),
        "rendered": find("rendered"),
        "chart": find("chart"),
        "service_account": sa,
        "jfrog_exchange_annotation": ann.group(1) if ann else None,
        "restarted_daemonset": bool(re.search(r"(?i)rollout restart|restarted", text or "")),
    }
