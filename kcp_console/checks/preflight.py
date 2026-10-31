"""Discover (read the cluster, the release and Artifactory) and Preflight (turn
those facts into pass/fail checks against a config). Read-only throughout.

Discover reads (knowledge.DISCOVER_READS): az aks show for the issuer and the
Workload Identity flags; the Helm release; the DaemonSets and the SA
annotation; the node list; Artifactory's OIDC providers and mappings. A
Forbidden answer is recorded with the read-only pipeline scenario to run instead.
"""

from __future__ import annotations

from pathlib import Path

from .. import logic, model, platforms
from ..jfrog import Jfrog
from ..k8s import Kube
from ..parse import helm
from . import writers


def discover(cfg: dict, shell, env=None, kubeconfig=None, tokens=None) -> dict:
    """kubeconfig: the console's file for this cluster (az aks get-credentials --file), so the reads hit the
    configured cluster and not whatever the operator's current kubectl context is."""
    cfg = model.merged(cfg)
    c, k = cfg["cluster"], cfg["k8s"]
    out = {"cluster": {}, "reads": []}
    show = platforms.show(cfg)
    aks = shell.run(show, timeout=90)
    if aks.ok:
        f = platforms.parse(cfg, aks.json({}) or {})
        out["cluster"] = {"oidc_enabled": f["oidc"], "issuer": f["issuer"], "workload_identity": f["wi"],
                          "kubernetes_version": f["version"], "location": f["location"], "platform": platforms.kind(cfg)}
    else:
        out["cluster"] = {"error": aks.error_text()}
    out["reads"].append({"what": " ".join(show[:3]), "ok": aks.ok})

    if kubeconfig is not None and aks.ok:
        Path(kubeconfig).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cred = platforms.credentials(cfg, kubeconfig)
        got = shell.run(cred, timeout=120, extra_env={"KUBECONFIG": str(kubeconfig)})
        out["reads"].append({"what": " ".join(cred[:4] if cred[0] == "gcloud" else cred[:3]), "ok": got.ok})
        if Path(kubeconfig).exists():
            Path(kubeconfig).chmod(0o600)
    kube = Kube(shell, kubeconfig=kubeconfig)
    ds = kube.daemonsets()
    out["daemonsets"] = writers.from_daemonsets(ds.get("data") or {}, cfg) if ds["ok"] else []
    sa = kube.service_account(k["namespace"], k["service_account"])
    ann = ((sa.get("data") or {}).get("metadata") or {}).get("annotations") or {}
    out["service_account"] = {"exists": sa["ok"], "annotation": ann.get("JFrogExchange")}
    nodes = kube.nodes()
    out["nodes"] = [n["metadata"]["name"] for n in (nodes.get("data") or {}).get("items", [])] if nodes["ok"] else []
    rel = kube.releases(k["namespace"])
    mine = next((r for r in rel.get("data") or [] if r.get("name") == k["release"]), None) if rel["ok"] else None
    out["release"] = mine
    vals = kube.values(k["release"], k["namespace"]) if mine else {"ok": False}
    man = kube.manifest(k["release"], k["namespace"]) if mine else {"ok": False}
    out["helm_values"] = vals.get("data") if vals["ok"] else ""
    out["manifest"] = man.get("data") if man["ok"] else ""
    for r in (ds, sa, nodes, rel, vals, man):
        if "what" in r:
            out["reads"].append({k2: r.get(k2) for k2 in ("what", "ok", "forbidden", "fallback", "user_principal", "error")
                                 if r.get(k2) is not None})

    jf = Jfrog(shell, cfg["jfrog"]["host"], env=env, tokens=tokens)
    out["jfrog_token_error"] = jf.token_error()
    out["jfrog_token_present"] = not out["jfrog_token_error"]
    prov = jf.providers()
    out["providers"] = prov["json"] if prov["ok"] and isinstance(prov["json"], list) else []
    out["reads"].append({"what": "GET /access/api/v1/oidc", "ok": prov["ok"], "status": prov["status"]})
    out["mappings"] = {}
    name = model.provider_name(cfg)
    if any(p.get("name") == name for p in out["providers"]):
        m = jf.mappings(name)
        out["mappings"][name] = m["json"] if m["ok"] and isinstance(m["json"], list) else []
        out["reads"].append({"what": f"GET identity_mappings of {name}", "ok": m["ok"], "status": m["status"]})
    return out


def _c(cid, title, status, detail, fix=None, fallback=None):
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix, "fallback": fallback}


def preflight(cfg: dict, facts: dict) -> dict:
    cfg = model.merged(cfg)
    cl, checks = facts.get("cluster") or {}, []
    gke = platforms.kind(cfg) == "gke"
    if gke:
        cfg["cluster"]["oidc_issuer"] = platforms.issuer(cfg)
    if cl.get("error"):
        checks.append(_c("aks", "Read the cluster", "fail", cl["error"]))
    elif gke:
        # A GKE cluster always has its issuer; Workload Identity is what Option B needs switched on.
        checks.append(_c("workload-identity", "Workload Identity enabled", "pass" if cl.get("workload_identity") else "fail",
                         f"workloadIdentityConfig.workloadPool {'set' if cl.get('workload_identity') else 'not set'}",
                         None if cl.get("workload_identity") else "Set up turns it on (gcloud container clusters update --workload-pool)"))
    else:
        checks.append(_c("oidc-issuer", "OIDC issuer enabled", "pass" if cl.get("oidc_enabled") else "fail",
                         f"oidcIssuerProfile.enabled = {str(cl.get('oidc_enabled')).lower()}",
                         None if cl.get("oidc_enabled") else "az aks update --enable-oidc-issuer --enable-workload-identity (shown, never run)"))
        checks.append(_c("workload-identity", "Workload Identity enabled", "pass" if cl.get("workload_identity") else "fail",
                         f"securityProfile.workloadIdentity.enabled = {str(cl.get('workload_identity')).lower()}",
                         None if cl.get("workload_identity") else "az aks update --enable-oidc-issuer --enable-workload-identity (shown, never run)"))
        same = (cl.get("issuer") or "").rstrip("/") == (cfg["cluster"]["oidc_issuer"] or "").rstrip("/")
        checks.append(_c("issuer", "Issuer captured", "pass" if same else "warn",
                         f"cluster issuer {cl.get('issuer')}" + ("" if same else f"; the config has {cfg['cluster']['oidc_issuer']}"),
                         None if same else "Use the discovered issuer in the config (Configure → Cluster)."))
    name = model.provider_name(cfg)
    providers = facts.get("providers") or []
    p = next((x for x in providers if x.get("name") == name), None)
    if p is None:
        checks.append(_c("provider", f"OIDC provider {name}", "warn", "not created yet: Phase 3 creates it (JFrog write, approval)"))
    else:
        drift = (p.get("issuer_url") or "").rstrip("/") != (cl.get("issuer") or cfg["cluster"]["oidc_issuer"]).rstrip("/")
        checks.append(_c("provider", f"OIDC provider {name}", "fail" if drift else "pass",
                         f"issuer_url {p.get('issuer_url')}" + (" differs from the cluster's issuer (issuer drift)" if drift else " matches the cluster"),
                         "An existing provider with a different issuer is refused, never reused: pick another name." if drift else None))
    protected = set(cfg["jfrog"]["protected_providers"])
    foreign = [x["name"] for x in providers if x.get("name") != name]
    if foreign:
        checks.append(_c("foreign-providers", "Other OIDC providers on this instance", "info",
                         ", ".join(f"{f}{' (protected: another cluster; leave alone)' if f in protected else ''}" for f in foreign)))
    maps = (facts.get("mappings") or {}).get(name)
    if maps is not None:
        sub = model.subject(cfg)
        ok = [m for m in maps if (m.get("claims") or {}).get("sub") == sub]
        checks.append(_c("mapping", "Identity mapping", "pass" if ok else "fail",
                         f"{len(maps)} mapping(s); " + (f"{ok[0]['name']} maps {sub} to {(ok[0].get('token_spec') or {}).get('username')}"
                                                        if ok else f"none has sub = {sub}")))
    sa = facts.get("service_account") or {}
    checks.append(_c("sa", f"ServiceAccount {cfg['k8s']['service_account']}",
                     "pass" if sa.get("annotation") == "true" else "fail" if sa.get("exists") else "warn",
                     f"JFrogExchange: {sa.get('annotation')!r}" if sa.get("exists") else "not readable or not created",
                     None if sa.get("annotation") == "true" else "Phase 2: annotate it JFrogExchange: \"true\"; the test pod must run in the same namespace."))
    for w in writers.check(facts.get("daemonsets") or []):
        checks.append(w)
    rel = facts.get("release")
    if rel:
        live = helm.values_summary(facts.get("helm_values") or "")
        want = helm.values_summary(model.render_values(cfg))
        diff = [f for f in ("artifactory_url", "match_images", "provider_name", "default_cache_duration")
                if live.get(f) not in (None, []) and live.get(f) != want.get(f)]
        checks.append(_c("release", f"Helm release {cfg['k8s']['release']}", "fail" if diff else "pass",
                         f"chart {rel.get('chart')}, revision {rel.get('revision')}" +
                         (f"; live values differ from the committed file on {', '.join(diff)}" if diff else "; live values match the committed file"),
                         "Re-run INSTALL_JFROG_PROVIDER with the committed values." if diff else None))
    else:
        checks.append(_c("release", f"Helm release {cfg['k8s']['release']}", "info", "not installed (or not readable)"))
    for r in facts.get("reads") or []:
        if r.get("forbidden"):
            who = f" (attributed to user {r['user_principal']})" if r.get("user_principal") else ""
            checks.append(_c(f"forbidden-{r['what']}", f"Read {r['what']}", "forbidden",
                             f"Forbidden{who}: personal accounts are read-only and namespaced.", fallback=r.get("fallback")))
    checks.append(_c("jfrog-token", "Short-lived JFrog token (OIDC exchange)", "pass" if facts.get("jfrog_token_present") else "warn",
                     "minted (never shown or stored)" if facts.get("jfrog_token_present")
                     else f"{facts.get('jfrog_token_error') or 'not minted'}: Artifactory reads will be anonymous"))
    issues = model.validate(cfg, providers or None)
    errs = model.errors(issues)
    checks.append(_c("values", "Config and values file", "fail" if errs else "pass",
                     "; ".join(i["message"] for i in errs) or f"{len(issues)} warning(s)" if issues else "no issues"))
    order = {"fail": 0, "forbidden": 1, "warn": 2, "info": 3, "pass": 4}
    status = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] in ("warn", "forbidden") for c in checks) else "pass"
    return {"status": status, "checks": sorted(checks, key=lambda c: order.get(c["status"], 9))}
