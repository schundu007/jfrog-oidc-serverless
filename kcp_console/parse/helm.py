"""Helm layers: the values file, `helm get values -a`, and `helm get manifest`.

All three reduce to the same summary dict so the three-layer diff can compare
them field by field: artifactory_url, match_images, provider_name,
default_cache_duration. The manifest is read for the rendered values (the
chart renders them into a ConfigMap/DaemonSet) and for its RBAC objects.
"""

from __future__ import annotations

import re

from . import yamlish


def values_summary(text_or_doc) -> dict:
    doc = yamlish.load(text_or_doc) if isinstance(text_or_doc, str) else (text_or_doc or {})
    doc = doc or {}
    pcs = doc.get("providerConfig") or []
    pc = pcs[0] if isinstance(pcs, list) and pcs and isinstance(pcs[0], dict) else {}
    azure = pc.get("azure") or {}
    return {
        "artifactory_url": pc.get("artifactoryUrl"),
        "match_images": [str(m) for m in pc.get("matchImages") or []],
        "provider_name": azure.get("jfrog_oidc_provider_name"),
        "azure_app_audience": azure.get("azure_app_audience"),
        "default_cache_duration": pc.get("defaultCacheDuration"),
        "token_attributes_enabled": (pc.get("tokenAttributes") or {}).get("enabled"),
        "download_url": doc.get("downloadUrl"),
        "rbac_create": (doc.get("rbac") or {}).get("create"),
        "update_strategy": doc.get("updateStrategy"),
        "node_affinity_label": _affinity_key(doc),
    }


def _affinity_key(doc: dict):
    try:
        terms = doc["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        return terms[0]["matchExpressions"][0]["key"]
    except (KeyError, IndexError, TypeError):
        return None


_RENDERED = {
    "artifactory_url": re.compile(r"""\bartifactory_url["']?\s*[:=]\s*["']?(?P<v>[^"'\s,}]+)"""),
    "provider_name": re.compile(r"""\bjfrog_oidc_provider_name["']?\s*[:=]\s*["']?(?P<v>[^"'\s,}]+)"""),
    "default_cache_duration": re.compile(r"""\bdefaultCacheDuration["']?\s*[:=]\s*["']?(?P<v>[^"'\s,}]+)"""),
}
_NAME_VALUE = re.compile(r"""-\s*name:\s*["']?(?P<k>artifactory_url|jfrog_oidc_provider_name)["']?\s*\n\s*value:\s*["']?(?P<v>[^"'\s]+)""")


def manifest_summary(text: str) -> dict:
    """Rendered values from `helm get manifest`. The chart renders the provider
    config as text inside a ConfigMap, so this reads it with patterns rather
    than relying on one document shape."""
    out = {"artifactory_url": None, "match_images": [], "provider_name": None, "default_cache_duration": None}
    text = text or ""
    for m in _NAME_VALUE.finditer(text):
        key = "artifactory_url" if m.group("k") == "artifactory_url" else "provider_name"
        out[key] = out[key] or m.group("v")
    for key, rx in _RENDERED.items():
        if out.get(key) is None and (m := rx.search(text)):
            out[key] = m.group("v")
    mi = re.search(r"matchImages:\s*\n((?:\s*-\s*\S+\s*\n?)+)", text)
    if mi:
        out["match_images"] = [x.strip().strip("\"'") for x in re.findall(r"-\s*(\S+)", mi.group(1))]
    else:
        flow = re.search(r"matchImages[\"']?\s*:\s*\[([^\]]*)\]", text)
        if flow:
            out["match_images"] = [x.strip().strip("\"'") for x in flow.group(1).split(",") if x.strip()]
    chart = re.search(r"helm\.sh/chart:\s*(\S+)", text)
    out["chart"] = chart.group(1) if chart else None
    return out


def rbac_objects(manifest_text: str) -> list:
    """Every Role/ClusterRole in the manifest with each rule's verbs and resources."""
    out = []
    for doc in yamlish.load_all(manifest_text or ""):
        if not isinstance(doc, dict) or doc.get("kind") not in ("Role", "ClusterRole"):
            continue
        rules = []
        for r in doc.get("rules") or []:
            if isinstance(r, dict):
                rules.append({"apiGroups": r.get("apiGroups") or [""], "resources": r.get("resources") or [],
                              "verbs": r.get("verbs") or [], "resourceNames": r.get("resourceNames") or []})
        out.append({"kind": doc["kind"], "name": (doc.get("metadata") or {}).get("name"), "rules": rules})
    return out


def broad_rbac(objects: list) -> list:
    """Rules that deserve a reviewer's attention: wildcards, secrets, write verbs on nodes."""
    flags = []
    for o in objects:
        for r in o["rules"]:
            if "*" in r["verbs"] or "*" in r["resources"]:
                flags.append(f"{o['kind']} {o['name']}: wildcard in {r}")
            if "secrets" in r["resources"]:
                flags.append(f"{o['kind']} {o['name']}: grants {', '.join(r['verbs'])} on secrets")
            if "nodes" in r["resources"] and set(r["verbs"]) & {"update", "patch", "delete", "create"}:
                flags.append(f"{o['kind']} {o['name']}: writes nodes ({', '.join(r['verbs'])})")
    return flags


def release_meta(text: str) -> dict:
    """Chart name/version and revision from `helm list -o json` / `helm history` / `helm status` text."""
    out = {}
    if m := re.search(r"jfrog-credential-provider-(\d+\.\d+\.\d+)", text or ""):
        out["chart_version"] = m.group(1)
    if m := re.search(r"(?i)\brevision\W+(\d+)", text or ""):
        out["revision"] = int(m.group(1))
    if m := re.search(r"(?i)app[_ ]version\W+([\d.]+)", text or ""):
        out["app_version"] = m.group(1)
    return out
