"""Image-pull credential rotator.

Platforms that pull images or packages *before* your code runs (Artifact Registry
remote repos, Container Apps registries, Functions custom containers) only accept
a static username/password. This job closes that gap: it runs on a schedule as
its own workload identity, exchanges that identity for a short-lived, read-only
JFrog token, and writes it where the platform reads it.

ROTATOR_TARGET=gcp
    JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE
    GCP_SECRET            projects/<p>/secrets/<name>
    AR_REPOSITORIES       comma-separated projects/<p>/locations/<l>/repositories/<r>
    KEEP_VERSIONS         live secret versions to keep; older ones are destroyed (default 2)

ROTATOR_TARGET=azure
    JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE (api://<app-id>), AZURE_CLIENT_ID
    KEY_VAULT_URI         https://<vault>.vault.azure.net
    KEY_VAULT_SECRET      secret name
    FUNCTION_APP_IDS      optional, comma-separated ARM IDs of Premium/Dedicated
                          function apps whose Key Vault references to refresh now
                          (otherwise App Service picks up the new version within 24h)

Common:
    MIN_TTL_SECONDS       fail if JFrog issues a token shorter than this (default 1800)
    DRY_RUN=1             exchange only; write nothing

Exit code is non-zero on any failure so the scheduler marks the execution failed;
alert on that and on secret age, not on downstream ImagePullBackOff.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from jfrog_oidc import AzureManagedIdentitySource, GcpMetadataSource, JFrogTokenProvider


def log(event: str, **fields) -> None:
    # One JSON line per event; Cloud Logging and Log Analytics both parse it.
    print(json.dumps({"severity": fields.pop("severity", "INFO"), "event": event, **fields}), flush=True)


def http(method: str, url: str, token: str, body: dict | None = None, timeout: float = 30.0, sleep=time.sleep) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            if (exc.code == 429 or exc.code >= 500) and attempt < 3:
                sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < 3:
                sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} unreachable: {exc}") from exc
    raise RuntimeError("unreachable")


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


# --------------------------------------------------------------------------- GCP

# Endpoints are overridable only so the test suite can point them at a fake.
SM = os.environ.get("SECRET_MANAGER_API", "https://secretmanager.googleapis.com/v1")
AR = os.environ.get("ARTIFACT_REGISTRY_API", "https://artifactregistry.googleapis.com/v1")


def rotate_gcp(provider: JFrogTokenProvider, dry_run: bool, clock=time.time, sleep=time.sleep) -> None:
    secret = require("GCP_SECRET")
    repos = [r.strip() for r in require("AR_REPOSITORIES").split(",") if r.strip()]
    keep = int(os.environ.get("KEEP_VERSIONS", "2"))
    update_mask = os.environ.get(
        "AR_UPDATE_MASK",
        "remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.username,"
        "remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.passwordSecretVersion",
    )

    tok = provider.token()
    check_ttl(tok, clock)
    if dry_run:
        log("dry_run", username=tok.username, expires_at=int(tok.expires_at))
        return

    gcp = provider.source if isinstance(provider.source, GcpMetadataSource) else GcpMetadataSource()
    api_token = gcp.get_access_token()

    data = tok.access_token.encode()
    added = http("POST", f"{SM}/{secret}:addVersion", api_token, {
        "payload": {"data": base64.b64encode(data).decode()},
    }, sleep=sleep)
    version = added["name"]
    log("secret_version_added", version=version, username=tok.username, expires_at=int(tok.expires_at))

    for repo in repos:
        body = {
            "remoteRepositoryConfig": {
                "upstreamCredentials": {
                    "usernamePasswordCredentials": {
                        "username": tok.username,
                        "passwordSecretVersion": version,
                    }
                }
            }
        }
        query = urllib.parse.urlencode({"updateMask": update_mask})
        http("PATCH", f"{AR}/{repo}?{query}", api_token, body, sleep=sleep)
        log("repository_repointed", repository=repo, version=version)

    # Destroy everything older than the newest `keep` live versions. Disabled
    # versions are still billed, and hourly rotation would add ~720 a month.
    # The previous version stays so an upstream fetch that already resolved it
    # does not fail mid-pull. Version 1 is Terraform's bootstrap placeholder;
    # leave it so Terraform does not plan to recreate it.
    listed = http("GET", f"{SM}/{secret}/versions?pageSize=100", api_token, sleep=sleep)
    live = [v for v in listed.get("versions", []) if v.get("state") != "DESTROYED"]
    live.sort(key=lambda v: int(v["name"].rsplit("/", 1)[1]), reverse=True)
    for stale in live[keep:]:
        if stale["name"].endswith("/versions/1"):
            continue
        http("POST", f"{SM}/{stale['name']}:destroy", api_token, {}, sleep=sleep)
        log("secret_version_destroyed", version=stale["name"])


# ------------------------------------------------------------------------- Azure

KV_API = "7.4"
ARM = os.environ.get("ARM_API", "https://management.azure.com")
ARM_RESOURCE = "https://management.azure.com/"


def rotate_azure(provider: JFrogTokenProvider, dry_run: bool, clock=time.time, sleep=time.sleep) -> None:
    vault = require("KEY_VAULT_URI").rstrip("/")
    name = require("KEY_VAULT_SECRET")
    function_apps = [a.strip() for a in os.environ.get("FUNCTION_APP_IDS", "").split(",") if a.strip()]

    tok = provider.token()
    check_ttl(tok, clock)
    if dry_run:
        log("dry_run", username=tok.username, expires_at=int(tok.expires_at))
        return

    msi = provider.source if isinstance(provider.source, AzureManagedIdentitySource) else AzureManagedIdentitySource()
    kv_token = msi.get_access_token("https://vault.azure.net")
    result = http("PUT", f"{vault}/secrets/{name}?api-version={KV_API}", kv_token, {
        "value": tok.access_token,
        "contentType": "jfrog-access-token",
        "attributes": {"enabled": True, "exp": int(tok.expires_at)},
        "tags": {"username": tok.username, "rotated-by": "jfrog-oidc-rotator"},
    }, sleep=sleep)
    log("keyvault_secret_set", secret_id=result.get("id", "").rsplit("/", 1)[-1],
        username=tok.username, expires_at=int(tok.expires_at))

    if function_apps:
        arm_token = msi.get_access_token(ARM_RESOURCE)
        failures = 0
        for app_id in function_apps:
            try:
                http("POST", f"{ARM}{app_id}/config/configreferences/appsettings/refresh?api-version=2022-03-01", arm_token, {},
                     sleep=sleep)
                log("function_app_refreshed", app=app_id.rsplit("/", 1)[-1])
            except RuntimeError as exc:
                failures += 1
                log("function_app_refresh_failed", app=app_id.rsplit("/", 1)[-1], error=str(exc), severity="ERROR")
        if failures:
            raise SystemExit(f"{failures} function app refresh(es) failed")


# -------------------------------------------------------------------------- main


def check_ttl(tok, clock=time.time) -> None:
    min_ttl = int(os.environ.get("MIN_TTL_SECONDS", "1800"))
    remaining = tok.remaining(clock())
    if remaining < min_ttl:
        raise SystemExit(
            f"JFrog issued a token with {remaining:.0f}s left, below MIN_TTL_SECONDS={min_ttl}; "
            "raise expires_in on the identity mapping"
        )


def main(clock=time.time, sleep=time.sleep) -> int:
    """Run one rotation. clock/sleep are injectable so the console simulator can
    run this unmodified on virtual time; the defaults are the real ones."""
    target = require("ROTATOR_TARGET").lower()
    dry_run = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")
    if target == "gcp":
        source = GcpMetadataSource()
    elif target == "azure":
        source = AzureManagedIdentitySource()
    else:
        raise SystemExit("ROTATOR_TARGET must be gcp or azure")

    provider = JFrogTokenProvider.from_env(source=source, clock=clock, sleep=sleep)
    started = clock()
    try:
        (rotate_gcp if target == "gcp" else rotate_azure)(provider, dry_run, clock=clock, sleep=sleep)
    except SystemExit as exc:
        log("rotation_failed", error=str(exc), severity="ERROR")
        return 1
    except Exception as exc:  # noqa: BLE001 - report and fail the execution
        log("rotation_failed", error=f"{type(exc).__name__}: {exc}", severity="ERROR")
        return 1
    log("rotation_succeeded", target=target, seconds=round(clock() - started, 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
