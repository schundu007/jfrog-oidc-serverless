"""kubelet's /var/lib/kubelet/credential-provider-config.yaml (CredentialProviderConfig).

parse() returns the providers with the JFrog entry's env flattened. render()
writes the same layout the chart's setup step writes, including the env: items
whose `- name:` lines once tripped RESET_NODE_JFROG_ENTRY's safety check.
"""

from __future__ import annotations

from .. import logic
from . import yamlish

# Supplied by the chart on the node. AZURE.md's Option B sample also sets the
# audience in values; ours only sets tokenAttributes.enabled. Not a gap.
CHART_SUPPLIED_TOKEN_ATTRIBUTES = {
    "serviceAccountTokenAudience": "api://AzureADTokenExchange",
    "cacheType": "ServiceAccount",
    "requireServiceAccount": True,
    "requiredServiceAccountAnnotationKeys": ["JFrogExchange"],
    "optionalServiceAccountAnnotationKeys": ["azure.workload.identity/client-id"],
}


def parse(text: str) -> dict:
    doc = yamlish.load(text or "") or {}
    providers = doc.get("providers") if isinstance(doc, dict) else None
    out = []
    for p in providers or []:
        if not isinstance(p, dict):
            continue
        out.append({
            "name": p.get("name"),
            "matchImages": [str(m) for m in (p.get("matchImages") or [])],
            "defaultCacheDuration": p.get("defaultCacheDuration"),
            "apiVersion": p.get("apiVersion"),
            "env": logic.entry_env(p),
            "tokenAttributes": p.get("tokenAttributes") or {},
            "args": p.get("args") or [],
        })
    jfrog = [p for p in out if p["name"] == logic.JFROG_PROVIDER]
    return {"kind": doc.get("kind") if isinstance(doc, dict) else None, "providers": out,
            "provider_names": [p["name"] for p in out], "jfrog_entries": len(jfrog),
            "jfrog": jfrog[0] if jfrog else None}


def jfrog_summary(parsed: dict) -> dict:
    """The fields the three-layer diff compares, from the node file's JFrog entry."""
    j = (parsed or {}).get("jfrog")
    if not j:
        return {}
    return {
        "artifactory_url": j["env"].get("artifactory_url"),
        "match_images": j["matchImages"],
        "provider_name": j["env"].get("jfrog_oidc_provider_name"),
        "azure_app_audience": j["env"].get("azure_app_audience"),
        "default_cache_duration": j["defaultCacheDuration"],
        "disable_provider_autoupdate": j["env"].get("disable_provider_autoupdate"),
        "token_attributes": j["tokenAttributes"],
    }


def missing_chart_supplied(parsed: dict) -> list:
    """tokenAttributes keys the chart should have written but did not."""
    ta = ((parsed or {}).get("jfrog") or {}).get("tokenAttributes") or {}
    missing = []
    for k, want in CHART_SUPPLIED_TOKEN_ATTRIBUTES.items():
        have = ta.get(k)
        if isinstance(want, list):
            if not isinstance(have, list) or not set(want) <= set(have):
                missing.append(k)
        elif have != want:
            missing.append(k)
    return missing


def jfrog_entry(artifactory_url: str, match_images: list, provider_name: str, cache: str,
                audience: str = "api://AzureADTokenExchange") -> dict:
    """The provider entry the chart's setup step writes, as a dict."""
    return {
        "name": logic.JFROG_PROVIDER,
        "apiVersion": "credentialprovider.kubelet.k8s.io/v1",
        "matchImages": list(match_images),
        "defaultCacheDuration": cache,
        "env": [
            {"name": "artifactory_url", "value": artifactory_url},
            {"name": "azure_app_audience", "value": audience},
            {"name": "jfrog_oidc_provider_name", "value": provider_name},
            {"name": "disable_provider_autoupdate", "value": "true"},
        ],
        "tokenAttributes": dict(CHART_SUPPLIED_TOKEN_ATTRIBUTES),
    }


ACR_ENTRY = {
    "name": logic.ACR_PROVIDER,
    "apiVersion": "credentialprovider.kubelet.k8s.io/v1",
    "matchImages": ["*.azurecr.io", "*.azurecr.cn", "*.azurecr.de", "*.azurecr.us"],
    "defaultCacheDuration": "10m",
    "args": ["/etc/kubernetes/azure.json"],
}


def _q(v) -> str:
    s = str(v)
    return f'"{s}"' if s in ("true", "false") else s


def render(providers: list) -> str:
    """Write a CredentialProviderConfig the way the node file looks."""
    out = ["apiVersion: kubelet.config.k8s.io/v1", "kind: CredentialProviderConfig", "providers:"]
    for p in providers:
        out.append(f"  - name: {p['name']}")
        out.append(f"    apiVersion: {p.get('apiVersion') or 'credentialprovider.kubelet.k8s.io/v1'}")
        out.append("    matchImages:")
        out += [f'      - "{m}"' for m in p.get("matchImages") or []]
        if p.get("defaultCacheDuration"):
            out.append(f"    defaultCacheDuration: {p['defaultCacheDuration']}")
        if p.get("args"):
            out.append("    args:")
            out += [f"      - {a}" for a in p["args"]]
        env = p.get("env")
        if env:
            items = env if isinstance(env, list) else [{"name": k, "value": v} for k, v in env.items()]
            out.append("    env:")
            for e in items:
                out.append(f"      - name: {e['name']}")
                out.append(f"        value: {_q(e['value'])}")
        ta = p.get("tokenAttributes")
        if ta:
            out.append("    tokenAttributes:")
            for k, v in ta.items():
                if isinstance(v, list):
                    out.append(f"      {k}:")
                    out += [f"        - {i}" for i in v]
                else:
                    out.append(f"      {k}: {str(v).lower() if isinstance(v, bool) else v}")
    return "\n".join(out) + "\n"
