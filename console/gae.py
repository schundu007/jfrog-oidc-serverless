"""App Engine standard: the commands and JSON shapes the steps share (Discover, Wire, Verify, live status).

An App Engine service has no service-level environment: each version carries its own `envVariables`, set only
by a deploy (app.yaml `env_variables`). So the version that serves the traffic is the one read, and the JFrog
variables are written into app.yaml by the stack's build-deploy pipeline, never updated in place.
"""

from __future__ import annotations

KIND = "gcp_app_engine"
DEFAULT = "default"   # the service an app.yaml without `service:` deploys to


def service_argv(project: str, name: str) -> list:
    return ["gcloud", "app", "services", "describe", name, "--project", project, "--format=json"]


def version_argv(project: str, name: str, version: str) -> list:
    return ["gcloud", "app", "versions", "describe", version, "--service", name, "--project", project, "--format=json"]


def app_argv(project: str) -> list:
    return ["gcloud", "app", "describe", "--project", project, "--format=value(defaultHostname)"]


def services_list_argv(project: str) -> list:
    return ["gcloud", "app", "services", "list", "--project", project, "--format=json"]


def serving_version(service: dict) -> str | None:
    """The version with the largest traffic share, None when nothing serves."""
    alloc = ((service or {}).get("split") or {}).get("allocations") or {}
    best = max(alloc.items(), key=lambda kv: (float(kv[1] or 0), kv[0]), default=None)
    return best[0] if best and float(best[1] or 0) > 0 else None


def service_url(host: str, name: str) -> str:
    """https URL of a service from the app's default host name (the default service is the host itself)."""
    host = host.strip()
    return f"https://{host}" if name == DEFAULT else f"https://{name}-dot-{host}"


def serving(sh, project: str, name: str):
    """(version JSON, None) for the serving version, or (None, the failed result or a reason)."""
    res = sh.run(service_argv(project, name), timeout=90)
    if not res.ok:
        return None, res
    version = serving_version(res.json() or {})
    if not version:
        return None, "the service has no version serving traffic"
    res = sh.run(version_argv(project, name, version), timeout=90)
    if not res.ok:
        return None, res
    return res.json() or {}, None


def env_of(version: dict) -> dict:
    return dict((version or {}).get("envVariables") or {})
