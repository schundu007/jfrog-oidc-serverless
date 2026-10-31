"""Cloud Run resource types, one place: which gcloud command group each workload kind lives in, and how
to read a described or listed item's container and service account whatever API shape it comes in.

Jobs and services come back in the v1 (Knative) shape: metadata.name, spec.template.spec(.template.spec
for jobs). Worker pools exist only in the v2 API and only under `gcloud beta run worker-pools` (gcloud
555: no GA group); their items are read in the v2 shape (name = projects/P/locations/R/workerPools/N,
template.containers, template.serviceAccount) or in the v1 shape, whichever is there.
"""

from __future__ import annotations

RESOURCE = {"gcp_run_job": "jobs", "gcp_run_service": "services", "gcp_run_function": "services",
            "gcp_run_worker_pool": "worker-pools"}
COLLECTIONS = ("services", "jobs", "worker-pools")
LOCATION = "cloud.googleapis.com/location"


def group(resource: str) -> list:
    """The gcloud command group for a collection: worker pools are beta only."""
    return ["gcloud", "beta", "run", resource] if resource == "worker-pools" else ["gcloud", "run", resource]


def _dig(d, *keys):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return None
    return d


def pod_spec(item: dict, resource: str) -> dict:
    """{containers, serviceAccountName} of a described or listed item, in one shape."""
    item = item or {}
    if resource == "jobs":
        spec = _dig(item, "spec", "template", "spec", "template", "spec")
    else:
        spec = _dig(item, "spec", "template", "spec")
    if isinstance(spec, dict) and spec.get("containers") is not None:
        return {"containers": spec.get("containers") or [], "serviceAccountName": spec.get("serviceAccountName") or ""}
    t = item.get("template") or {}   # v2 shape
    if resource == "jobs":
        t = t.get("template") or t
    return {"containers": t.get("containers") or [], "serviceAccountName": t.get("serviceAccount") or ""}


def name_region(item: dict, default_region: str = "") -> tuple:
    """(name, region) of a listed item, from v1 metadata or the v2 resource name."""
    item = item or {}
    md = item.get("metadata") or {}
    if md.get("name"):
        return md["name"], (md.get("labels") or {}).get(LOCATION) or default_region
    full = str(item.get("name") or "")
    parts = full.split("/")
    if len(parts) >= 6 and parts[2] == "locations":
        return parts[-1], parts[3]
    return full, default_region


def labels(item: dict) -> dict:
    item = item or {}
    return (item.get("metadata") or {}).get("labels") or item.get("labels") or {}
