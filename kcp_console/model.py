"""KCP console config: schema, sandbox-pilot defaults, values rendering and validators.

A config holds no secrets. The values file the console renders is the source
of truth for every security-relevant Helm setting; only the binary download
token is injected at run time (and never by the console: it is a secret
pipeline variable).
"""

from __future__ import annotations

import copy
import re

from . import knowledge, logic

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PLATFORMS = {"aks": True, "eks": False, "gke": False, "openshift": False}   # seam: only AKS is built
ENVIRONMENTS = ("SBX", "NPE", "PRD")
CACHE_WARN_SECONDS = 1800

SANDBOX_ISSUER = ("https://centralus.oic.prod-aks.azure.com/00000000-0000-0000-0000-0000000000b2/"
               "00000000-0000-0000-0000-0000000000c3/")


def default_config(name: str = "sandbox-pilot") -> dict:
    return {
        "name": name,
        "mode": "pilot",
        "platform": "aks",
        "option": "B",
        "cluster": {"name": "aks-sandbox-01", "resource_group": "rg-aks-sandbox",
                    "subscription_id": "00000000-0000-0000-0000-0000000000a1", "subscription_name": "sandbox-subscription",
                    "oidc_issuer": SANDBOX_ISSUER, "environment": "SBX",
                    "nodes": ["aks-nodepool1-12345678-vmss00000e", "aks-nodepool1-12345678-vmss00000g"]},
        "k8s": {"namespace": "jfrog", "service_account": "jfrog-provider-sa", "release": "jfrog-cred-provider",
                "daemonset": "jfrog-cred-provider-jfrog-credential-provider-daemonset",
                "test_pod": "secretless-pull-test", "stale_pods": ["pilot-test-pod"],
                "node_label": "credentialsProviderEnabled"},
        "jfrog": {"host": "example.jfrog.io", "oidc_provider": "aks-sandbox-oidc",
                  "mapping_name": "aks-workload-identity-mapping", "mapped_user": "kcp-pull-user",
                  "scope": "applied-permissions/user", "audience": "*@*", "expires_in": 3600,
                  "protected_providers": [p["name"] for p in knowledge.PROTECTED_PROVIDERS],
                  "test_repo": "aks-sandbox-test-docker-local",
                  "test_repo_host": "example-aks-sandbox-test-docker-local.jfrog.io",
                  "test_image": "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1",
                  "binary_repo": "aks-sandbox-generic-local",
                  "promote_source_repo": "containers-docker-local"},
        "values": {"artifactory_url": "example.jfrog.io",
                   "match_images": ["example-aks-sandbox-test-docker-local.jfrog.io"],
                   "default_cache_duration": "5h", "provider_name": "aks-sandbox-oidc",
                   "azure_app_audience": "api://AzureADTokenExchange", "token_attributes_enabled": True,
                   "download_url": ("https://example.jfrog.io/artifactory/aks-sandbox-generic-local/"
                                    "jfrog-credential-provider/1.4.0/jfrog-credential-provider-linux"),
                   "chart_repo": logic.APPROVED_CHART_REPO, "chart_name": logic.APPROVED_CHART_NAME,
                   "chart_version": "1.4.0", "binary_version": "1.4.0", "rbac_create": True,
                   "node_affinity_label": "credentialsProviderEnabled"},
        "template": {i["name"]: i["default"] for i in knowledge.TEMPLATE_INPUTS},
        "ado": {"organization": "", "project": "", "repo": "ArgoCD.Helm.Template", "branch": "feature/jfrog-kcp",
                "pilot_pipeline": "argocd-utility", "cluster_pipeline": "jfrog-kcp-cluster",
                "mappings_pipeline": "jfrog-kcp-mappings",
                "service_connection": "NPE AKS service connection (the one argocd-utility already uses)",
                "key_vault": "kv-ci-secrets", "key_vault_secret": "artifactory-download-token",
                "values_path": knowledge.VALUES_FILE_PATH},
        "overrides": {"DiagnosticImage": "mcr.microsoft.com/azurelinux/base/core:3.0", "ChartRepo": logic.APPROVED_CHART_REPO},
        "demo_mode": False,
    }


# What a new configuration starts with: every cluster-, tenant- and JFrog-specific field empty
# (the form shows placeholders); chart, audience and Kubernetes names keep the chart's defaults.
BLANK_FIELDS = {
    "cluster": {"name": "", "resource_group": "", "subscription_id": "", "subscription_name": "", "oidc_issuer": "", "nodes": []},
    "k8s": {"stale_pods": []},
    "jfrog": {"host": "", "oidc_provider": "", "mapping_name": "", "mapped_user": "", "protected_providers": [],
              "test_repo": "", "test_repo_host": "", "test_image": "", "binary_repo": "", "promote_source_repo": ""},
    "values": {"artifactory_url": "", "match_images": [], "provider_name": "", "download_url": ""},
    "ado": {"organization": "", "project": "", "key_vault": "", "key_vault_secret": ""},
}


def blank_config(name: str = "") -> dict:
    cfg = default_config(name)
    for group, fields in BLANK_FIELDS.items():
        cfg[group].update(copy.deepcopy(fields))
    cfg["template"] = {k: ("" if k in ("ClusterName", "SubscriptionId", "MatchImages", "BinaryMirrorRepo", "OidcProviderName", "ArtifactoryHost",
                                       "TestImage") else v) for k, v in cfg["template"].items()}
    return cfg


def template_defaults_for(cfg: dict) -> dict:
    """Template inputs prefilled from the pilot fields where they mean the same thing."""
    t = dict(cfg.get("template") or {})
    t.setdefault("ClusterName", "")
    t["ClusterName"] = t["ClusterName"] or cfg["cluster"]["name"]
    t["SubscriptionId"] = t.get("SubscriptionId") or cfg["cluster"]["subscription_id"]
    return t


def get_path(obj, path: str):
    for k in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def merged(cfg: dict) -> dict:
    """The defaults with cfg laid over them, so partial configs validate."""
    base = default_config(cfg.get("name") or "sandbox-pilot")

    def deep(b, o):
        for k, v in (o or {}).items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                deep(b[k], v)
            else:
                b[k] = copy.deepcopy(v)
        return b
    return deep(base, cfg)


def provider_name(cfg: dict) -> str:
    """Blank OidcProviderName defaults to aks-<ClusterName>-oidc."""
    if cfg.get("mode") == "template":
        name = (cfg.get("template") or {}).get("OidcProviderName") or ""
        return name or f"aks-{(cfg.get('template') or {}).get('ClusterName') or cfg['cluster']['name']}-oidc"
    return cfg["values"].get("provider_name") or f"aks-{cfg['cluster']['name']}-oidc"


def subject(cfg: dict) -> str:
    return f"system:serviceaccount:{cfg['k8s']['namespace']}:{cfg['k8s']['service_account']}"


# ------------------------------------------------------------------ rendering

def render_values(cfg: dict) -> str:
    """The values file in the reference layout (AZURE.md Option B). A blank downloadUrl is
    left out, so the chart's own default (releases.jfrog.io) applies."""
    v = cfg["values"]
    mi = "\n".join(f'      - "{m}"' for m in v["match_images"])
    return f'''providerConfig:
  - name: jfrog-credentials-provider
    artifactoryUrl: "{v['artifactory_url']}"
    matchImages:
{mi}
    defaultCacheDuration: {v['default_cache_duration']}
    tokenAttributes:
      enabled: {str(bool(v['token_attributes_enabled'])).lower()}
      serviceAccountTokenAudience: "{v['azure_app_audience']}"
    azure:
      enabled: true
      azure_app_audience: "{v['azure_app_audience']}"
      jfrog_oidc_provider_name: "{v['provider_name']}"
rbac:
  create: {str(bool(v['rbac_create'])).lower()}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: {v['node_affinity_label']}
              operator: In
              values:
                - "true"
''' + ('downloadUrl: "%s"\n' % v["download_url"] if v["download_url"] else "")


def values_sha256(cfg: dict) -> str:
    return logic.sha256_hex(render_values(cfg))


def provider_request(cfg: dict) -> dict:
    iss = cfg["cluster"]["oidc_issuer"]
    return {"name": provider_name(cfg), "issuer_url": iss, "provider_type": "Azure", "token_issuer": iss,
            "use_default_proxy": False, "description": "OIDC provider for Azure AKS Workload Identity"}


def mapping_request(cfg: dict) -> dict:
    j = cfg["jfrog"]
    return {"name": j["mapping_name"], "description": "Azure AKS Workload Identity mapping",
            "claims": {"iss": cfg["cluster"]["oidc_issuer"], "sub": subject(cfg)},
            "token_spec": {"username": j["mapped_user"], "scope": j["scope"], "audience": j.get("audience", "*@*"),
                           "expires_in": j["expires_in"]},
            "priority": 1}


def test_pod_yaml(cfg: dict, image: str = None, service_account: str = None) -> str:
    k = cfg["k8s"]
    return (f"apiVersion: v1\nkind: Pod\nmetadata:\n  name: {k['test_pod']}\n  namespace: {k['namespace']}\n"
            f"spec:\n  serviceAccountName: {service_account or k['service_account']}\n  imagePullSecrets: []\n"
            f"  containers:\n    - name: test\n      image: {image or cfg['jfrog']['test_image']}\n")


# ------------------------------------------------------------------ validation

def _issue(level, field, message):
    return {"level": level, "field": field, "message": message}


def validate(cfg: dict, providers: list = None) -> list:
    """Errors block Install and Run; warnings are shown. `providers` is the
    Artifactory provider list when known (from Discover)."""
    cfg = merged(cfg)
    out = []
    v, j, k, c = cfg["values"], cfg["jfrog"], cfg["k8s"], cfg["cluster"]
    if not NAME_RE.match(cfg.get("name") or ""):
        out.append(_issue("error", "name", "lowercase letters, digits and dashes (max 63)"))
    if not PLATFORMS.get(cfg.get("platform")):
        out.append(_issue("error", "platform", f"{cfg.get('platform')} is not built yet (only AKS)"))
    if cfg.get("option") != "B":
        out.append(_issue("error", "option", "only Option B is built; Option A is modelled for the A-vs-B decision"))

    if err := logic.bare_host_error(v["artifactory_url"]):
        out.append(_issue("error", "values.artifactory_url", f"artifactoryUrl {err}"))
    if not v["match_images"]:
        out.append(_issue("error", "values.match_images", "at least one registry host is required"))
    for m in v["match_images"]:
        if "://" in m or "/" in m:
            out.append(_issue("error", "values.match_images", f"{m}: a bare dedicated subdomain, no scheme or path"))
        elif m.lower() == (j["host"] or "").lower():
            out.append(_issue("warning", "values.match_images",
                              f"{m} is the platform host: the bare-domain path method does not work for local repos "
                              f"here; use the repo's dedicated subdomain"))
    images = [j["test_image"]] + ([cfg["template"].get("TestImage")] if cfg["template"].get("TestImage") else [])
    for img in images:
        if not any(logic.match_image(m, img) for m in v["match_images"]):
            out.append(_issue("error", "values.match_images",
                              f"{logic.image_host(img)} (image {img}) is not matched by matchImages: kubelet would "
                              f"never invoke the provider"))
    if v["download_url"] and (err := logic.download_url_problem(v["download_url"])):
        out.append(_issue("error", "values.download_url", f"downloadUrl {err}"))
    cache = logic.duration_seconds(v["default_cache_duration"])
    if cache is None:
        out.append(_issue("error", "values.default_cache_duration", "a Go duration such as 30m or 5h"))
    else:
        if cache > int(j["expires_in"]):
            out.append(_issue("warning", "values.default_cache_duration",
                              f"{v['default_cache_duration']} is longer than the mapping's expires_in "
                              f"({j['expires_in']}s). The pilot ran this way and pulled fine; whether kubelet can "
                              f"hand containerd an expired token inside the window is unverified (open item)"))
        if cache > CACHE_WARN_SECONDS:
            out.append(_issue("warning", "values.default_cache_duration",
                              f"{v['default_cache_duration']} is over 30m (the template default). The pilot's 5h was "
                              f"never tested against token expiry"))
    if v["chart_repo"].rstrip("/") != logic.APPROVED_CHART_REPO:
        out.append(_issue("error", "values.chart_repo",
                          f"chart repo must be {logic.APPROVED_CHART_REPO} (the guessed jfrog.github.io URL returned 404)"))
    if v["chart_name"] != logic.APPROVED_CHART_NAME:
        out.append(_issue("error", "values.chart_name", f"chart must be {logic.APPROVED_CHART_NAME} (singular)"))
    out.append(_issue("warning", "overrides.ChartRepo",
                      "the chart source is not on the enterprise approved-sources list yet (security review: sourcing)"))
    if v["provider_name"] in j["protected_providers"]:
        out.append(_issue("error", "values.provider_name",
                          f"{v['provider_name']} belongs to a different cluster and is protected: use this "
                          f"cluster's own provider"))
    if providers is not None:
        found = next((p for p in providers if p.get("name") == v["provider_name"]), None)
        if not found:
            out.append(_issue("error", "values.provider_name",
                              f"{v['provider_name']} does not exist in Artifactory yet (Phase 3 creates it)"))
        elif (found.get("issuer_url") or "").rstrip("/") != c["oidc_issuer"].rstrip("/"):
            out.append(_issue("error", "values.provider_name",
                              f"{v['provider_name']} exists with a different issuer ({found.get('issuer_url')}): "
                              f"refused, never reused"))

    if int(j["expires_in"]) > 3600:
        out.append(_issue("error", "jfrog.expires_in", "token lifetime at most 3600 seconds"))
    if "admin" in (j["scope"] or "").lower():
        out.append(_issue("error", "jfrog.scope", "no admin scope"))
    if c["environment"] not in ENVIRONMENTS:
        out.append(_issue("error", "cluster.environment", "SBX, NPE or PRD"))
    if not c["oidc_issuer"].startswith("https://"):
        out.append(_issue("error", "cluster.oidc_issuer", "the cluster's real issuer URL (Discover fills it)"))
    sc = cfg["ado"]["service_connection"].lower()
    if c["environment"] != "PRD" and re.search(r"\bprd\b|\bprod\b", sc):
        out.append(_issue("warning", "ado.service_connection",
                          "a prod-scoped connection borrowed from another pipeline failed the Key Vault lookup; use "
                          "the NPE connection this pipeline already uses for AKS access"))

    if cfg.get("mode") == "template":
        out += validate_template(cfg)
    return out


def validate_template(cfg: dict) -> list:
    t = template_defaults_for(cfg)
    out = []
    for req in ("ClusterName", "SubscriptionId"):
        if not t.get(req):
            out.append(_issue("error", f"template.{req}", "required"))
    if t.get("Environment") not in ENVIRONMENTS:
        out.append(_issue("error", "template.Environment", "SBX, NPE or PRD"))
    if err := logic.bare_host_error(t.get("ArtifactoryHost") or ""):
        out.append(_issue("error", "template.ArtifactoryHost", f"ArtifactoryHost {err}"))
    hosts = [h.strip() for h in (t.get("MatchImages") or "").split(",") if h.strip()]
    if not hosts:
        out.append(_issue("error", "template.MatchImages", "required for Install"))
    for h in hosts:
        if "://" in h or "/" in h:
            out.append(_issue("error", "template.MatchImages", f"{h}: dedicated subdomains, no scheme or path"))
    if not t.get("BinaryMirrorRepo"):
        out.append(_issue("error", "template.BinaryMirrorRepo", "required for Install; no default, on purpose"))
    cache = logic.duration_seconds(t.get("DefaultCacheDuration"))
    if cache is None or cache > 3600:
        out.append(_issue("error", "template.DefaultCacheDuration", "a duration inside the 1 hour token lifetime"))
    if not re.fullmatch(r"\d{1,3}%|\d+", str(t.get("MaxUnavailable") or "")):
        out.append(_issue("error", "template.MaxUnavailable", "a count or a percentage such as 10%"))
    try:
        if int(t.get("VerifySampleNodes")) < 1:
            raise ValueError
    except (TypeError, ValueError):
        out.append(_issue("error", "template.VerifySampleNodes", "at least 1"))
    if t.get("TestImage") and hosts and not any(logic.match_image(h, t["TestImage"]) for h in hosts):
        out.append(_issue("error", "template.TestImage", "its host is not in MatchImages"))
    for pin in ("ChartVersion", "BinaryVersion"):
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(t.get(pin) or "")):
            out.append(_issue("error", f"template.{pin}", "a pinned version such as 1.4.0"))
    return out


def errors(issues: list) -> list:
    return [i for i in issues if i["level"] == "error"]
