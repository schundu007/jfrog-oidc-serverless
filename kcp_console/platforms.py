"""What differs between clouds: reading the cluster, turning its workload identity on, getting
credentials, and the names GKE derives. AKS follows AZURE.md Option B, GKE follows GCP.md Option B
(Workload Identity: kubelet hands the pod's token, audience `artifactory`, to the provider, and JFrog
trusts the cluster's own issuer). Everything else in a run is shared."""

from __future__ import annotations

import json

GKE_ROLES = ("roles/iam.workloadIdentityUser", "roles/iam.serviceAccountOpenIdTokenCreator")
GSA_NAME = "jfrog-kcp"


def kind(cfg: dict) -> str:
    return cfg.get("platform") or "aks"


def _az(c: dict) -> list:
    return ["--resource-group", c["resource_group"], "--name", c["name"]] + (["--subscription", c["subscription_id"]] if c.get("subscription_id") else [])


def _gc(c: dict) -> list:
    return [c["name"], "--location", c["location"], "--project", c["project_id"]]


def issuer(cfg: dict) -> str:
    """GKE's issuer is fixed by the cluster's coordinates; AKS's comes from az aks show."""
    c = cfg["cluster"]
    if kind(cfg) == "gke":
        return f"https://container.googleapis.com/v1/projects/{c['project_id']}/locations/{c['location']}/clusters/{c['name']}"
    return c.get("oidc_issuer") or ""


def gsa_email(cfg: dict) -> str:
    c = cfg["cluster"]
    return c.get("google_service_account") or f"{GSA_NAME}@{c['project_id']}.iam.gserviceaccount.com"


def wi_member(cfg: dict) -> str:
    k = cfg["k8s"]
    return f"serviceAccount:{cfg['cluster']['project_id']}.svc.id.goog[{k['namespace']}/{k['service_account']}]"


def show(cfg: dict) -> list:
    c = cfg["cluster"]
    if kind(cfg) == "gke":
        return ["gcloud", "container", "clusters", "describe", *_gc(c), "--format=json"]
    return ["az", "aks", "show", *_az(c), "-o", "json"]


def parse(cfg: dict, d: dict) -> dict:
    """The facts a run and Discover need from the cluster read."""
    d = d or {}
    if kind(cfg) == "gke":
        pool = (d.get("workloadIdentityConfig") or {}).get("workloadPool") or ""
        return {"issuer": issuer(cfg), "oidc": True, "wi": bool(pool), "aad": False,
                "version": d.get("currentMasterVersion", ""), "location": d.get("location", ""),
                "autopilot": bool((d.get("autopilot") or {}).get("enabled"))}
    prof = d.get("oidcIssuerProfile") or {}
    return {"issuer": prof.get("issuerUrl") or "", "oidc": bool(prof.get("enabled")),
            "wi": bool(((d.get("securityProfile") or {}).get("workloadIdentity") or {}).get("enabled")),
            "aad": bool(d.get("aadProfile")), "version": d.get("kubernetesVersion", ""), "location": d.get("location", ""),
            "autopilot": False}


def enable_title(cfg: dict) -> str:
    return "Enable Workload Identity (only if off)" if kind(cfg) == "gke" else "Enable the OIDC issuer (only if off)"


def enable(cfg: dict) -> list:
    c = cfg["cluster"]
    if kind(cfg) == "gke":
        return ["gcloud", "container", "clusters", "update", *_gc(c), f"--workload-pool={c['project_id']}.svc.id.goog"]
    return ["az", "aks", "update", *_az(c), "--enable-oidc-issuer"]


def enabled(cfg: dict, facts: dict) -> bool:
    return facts.get("wi") if kind(cfg) == "gke" else facts.get("oidc")


def credentials(cfg: dict, kubeconfig) -> list:
    """gcloud writes to $KUBECONFIG, which every run sets to the console's own file for the cluster."""
    c = cfg["cluster"]
    if kind(cfg) == "gke":
        return ["gcloud", "container", "clusters", "get-credentials", *_gc(c)]
    return ["az", "aks", "get-credentials", *_az(c), "--file", str(kubeconfig), "--overwrite-existing"]


def required(cfg: dict) -> list:
    c = cfg["cluster"]
    if kind(cfg) == "gke":
        return [("cluster.name", c["name"]), ("cluster.project_id", c.get("project_id")), ("cluster.location", c.get("location"))]
    return [("cluster.name", c["name"]), ("cluster.resource_group", c["resource_group"])]


def gke_list(shell, used: set) -> dict:
    """The GKE clusters in every project gcloud can see (read-only). A project without the Kubernetes
    Engine API, or without access, is skipped. used: cluster names already in a config."""
    pr = shell.run(["gcloud", "projects", "list", "--format=json"], timeout=90)
    if pr.rc != 0:
        lines = (pr.stderr or pr.stdout or "").strip().splitlines()
        return {"clusters": [], "error": lines[-1] if lines else f"gcloud projects list: exit {pr.rc}"}
    try:
        projects = json.loads(pr.stdout or "[]")
    except ValueError:
        projects = []
    out = []
    for p in projects:
        pid = p.get("projectId", "")
        r = shell.run(["gcloud", "container", "clusters", "list", "--project", pid, "--format=json"], timeout=60)
        if r.rc != 0:
            continue
        try:
            items = json.loads(r.stdout or "[]")
        except ValueError:
            continue
        for c in items:
            out.append({"name": c.get("name", ""), "project_id": pid, "project_name": p.get("name", ""),
                        "location": c.get("location", ""), "version": c.get("currentMasterVersion", ""),
                        "status": c.get("status", ""), "nodes": c.get("currentNodeCount", 0),
                        "workload_identity": bool((c.get("workloadIdentityConfig") or {}).get("workloadPool")),
                        "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
                        "configured": (c.get("name") or "").lower() in used})
    return {"clusters": sorted(out, key=lambda x: (x["configured"], x["project_id"], x["name"])), "projects": len(projects)}
