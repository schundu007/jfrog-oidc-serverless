"""Identity sources: obtain a platform-signed OIDC token for the running workload.

Each source returns a raw JWT string signed by the cloud's identity platform.
Nothing here holds a long-lived secret; the token comes from a local endpoint
that only the workload itself can reach.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from .errors import IdentityError

DEFAULT_TIMEOUT = 5.0


class IdentitySource(Protocol):
    name: str

    def get_token(self, audience: str) -> str:  # pragma: no cover - protocol
        ...


# Local metadata endpoints must never go through an HTTP proxy. Built once:
# build_opener() creates an HTTPS handler that loads the CA bundle each time.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        raise IdentityError(f"identity endpoint returned HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise IdentityError(f"identity endpoint unreachable: {exc}") from exc


class GcpMetadataSource:
    """Google ID token from the metadata server.

    Works on Cloud Run services, Cloud Run jobs, Cloud Run functions (and GCE,
    GKE with Workload Identity). format=full adds email/email_verified claims;
    sub is the service account's numeric unique ID, which is what JFrog maps on.
    """

    name = "gcp"

    def __init__(self, metadata_host: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        host = metadata_host or os.environ.get("GCE_METADATA_HOST") or "metadata.google.internal"
        scheme = "" if host.startswith("http") else "http://"
        self._base = f"{scheme}{host}/computeMetadata/v1/instance/service-accounts/default"
        self._timeout = timeout

    def get_token(self, audience: str) -> str:
        query = urllib.parse.urlencode({"audience": audience, "format": "full"})
        raw = _get(f"{self._base}/identity?{query}", {"Metadata-Flavor": "Google"}, self._timeout)
        token = raw.decode().strip()
        if token.count(".") != 2:
            raise IdentityError("metadata server did not return a JWT")
        return token

    def get_access_token(self) -> str:
        """OAuth access token for Google APIs (used by the rotator)."""
        raw = _get(f"{self._base}/token", {"Metadata-Flavor": "Google"}, self._timeout)
        try:
            return json.loads(raw)["access_token"]
        except (ValueError, KeyError) as exc:
            raise IdentityError("metadata token response missing access_token") from exc


class AzureManagedIdentitySource:
    """Entra token for a managed identity.

    Container Apps, Functions and App Service expose IDENTITY_ENDPOINT and
    IDENTITY_HEADER; VMs use IMDS. Set AZURE_CLIENT_ID (or pass client_id) to pick
    a user-assigned identity when more than one is attached.

    audience here is the Entra *resource* to request, e.g. api://<app-client-id>.
    With requestedAccessTokenVersion = 2 on that app, the token's aud is the
    client ID and iss is https://login.microsoftonline.com/<tenant>/v2.0.
    """

    name = "azure"

    IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"

    def __init__(self, client_id: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        self._client_id = client_id or os.environ.get("AZURE_CLIENT_ID") or None
        self._endpoint = os.environ.get("IDENTITY_ENDPOINT")
        self._header = os.environ.get("IDENTITY_HEADER")
        self._timeout = timeout

    def _request(self, resource: str) -> dict:
        params = {"resource": resource}
        if self._client_id:
            params["client_id"] = self._client_id
        if self._endpoint and self._header:
            params["api-version"] = "2019-08-01"
            url = f"{self._endpoint}?{urllib.parse.urlencode(params)}"
            headers = {"X-IDENTITY-HEADER": self._header}
        else:
            params["api-version"] = "2018-02-01"
            url = f"{self.IMDS}?{urllib.parse.urlencode(params)}"
            headers = {"Metadata": "true"}
        raw = _get(url, headers, self._timeout)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise IdentityError("managed identity endpoint returned non-JSON") from exc

    def get_token(self, audience: str) -> str:
        body = self._request(audience)
        token = body.get("access_token")
        if not token:
            raise IdentityError("managed identity response missing access_token")
        return token

    def get_access_token(self, resource: str) -> str:
        """Entra token for an Azure API (Key Vault, ARM) — used by the rotator."""
        return self.get_token(resource)


class FileTokenSource:
    """Token read from a file the platform keeps fresh.

    Covers AKS workload identity (AZURE_FEDERATED_TOKEN_FILE), Kubernetes
    projected service-account tokens, and local testing. The audience is fixed
    by whatever wrote the file, so it is ignored here.
    """

    name = "file"

    def __init__(self, path: Optional[str] = None):
        self._path = path or os.environ.get("JFROG_OIDC_TOKEN_FILE") or os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        if not self._path:
            raise IdentityError("FileTokenSource needs a path (JFROG_OIDC_TOKEN_FILE)")

    def get_token(self, audience: str) -> str:
        try:
            with open(self._path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as exc:
            raise IdentityError(f"cannot read token file {self._path}: {exc}") from exc
        if not token:
            raise IdentityError(f"token file {self._path} is empty")
        return token


class AzureDevOpsSource:
    """ID token for an Azure DevOps pipeline job, issued for a service connection.

    The job POSTs to SYSTEM_OIDCREQUESTURI with its job token (SYSTEM_ACCESSTOKEN,
    mapped into the step's env) and gets a token with iss
    https://vstoken.dev.azure.com/<org-id> and sub sc://<org>/<project>/<connection>.
    The audience is fixed by Azure DevOps (api://AzureADTokenExchange), so the one
    passed in is ignored. The connection must be referenced by a task in the job
    (or authorized for the pipeline) before Azure DevOps will issue the token.
    """

    name = "ado"

    def __init__(self, service_connection_id: Optional[str] = None, timeout: float = 10.0):
        self._uri = os.environ.get("SYSTEM_OIDCREQUESTURI")
        self._job_token = os.environ.get("SYSTEM_ACCESSTOKEN")
        self._connection = service_connection_id or os.environ.get("JFROG_OIDC_ADO_SERVICE_CONNECTION_ID")
        self._timeout = timeout
        if not (self._uri and self._job_token):
            raise IdentityError("AzureDevOpsSource needs SYSTEM_OIDCREQUESTURI and SYSTEM_ACCESSTOKEN "
                                "(map $(System.AccessToken) into the step's env)")
        if not self._connection:
            raise IdentityError("AzureDevOpsSource needs a service connection ID (JFROG_OIDC_ADO_SERVICE_CONNECTION_ID)")

    def get_token(self, audience: str) -> str:
        if not self._uri.startswith("https://") and os.environ.get("JFROG_OIDC_ALLOW_HTTP") != "1":
            raise IdentityError("SYSTEM_OIDCREQUESTURI must be https")
        query = urllib.parse.urlencode({"api-version": "7.1", "serviceConnectionId": self._connection})
        url = f"{self._uri}{'&' if '?' in self._uri else '?'}{query}"
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={
            "Authorization": f"Bearer {self._job_token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise IdentityError(f"Azure DevOps OIDC endpoint returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise IdentityError(f"Azure DevOps OIDC endpoint unreachable: {exc}") from None
        try:
            token = json.loads(raw)["oidcToken"]
        except (ValueError, KeyError, TypeError):
            raise IdentityError("Azure DevOps OIDC response missing oidcToken") from None
        if not isinstance(token, str) or token.count(".") != 2:
            raise IdentityError("Azure DevOps did not return a JWT")
        return token


def detect_source() -> IdentitySource:
    """Pick a source from JFROG_OIDC_SOURCE or the runtime's environment."""
    choice = (os.environ.get("JFROG_OIDC_SOURCE") or "auto").lower()
    if choice == "gcp":
        return GcpMetadataSource()
    if choice == "azure":
        return AzureManagedIdentitySource()
    if choice == "file":
        return FileTokenSource()
    if choice == "ado":
        return AzureDevOpsSource()
    if choice != "auto":
        raise IdentityError(f"unknown JFROG_OIDC_SOURCE={choice!r} (use auto, gcp, azure, ado or file)")

    if os.environ.get("JFROG_OIDC_TOKEN_FILE"):
        return FileTokenSource()
    # Azure DevOps agents often run on Azure VMs: check the pipeline first.
    if os.environ.get("SYSTEM_OIDCREQUESTURI"):
        return AzureDevOpsSource()
    if os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
        return AzureManagedIdentitySource()
    # Cloud Run services/functions set K_SERVICE; jobs set CLOUD_RUN_JOB.
    if any(os.environ.get(v) for v in ("K_SERVICE", "CLOUD_RUN_JOB", "FUNCTION_TARGET", "GCE_METADATA_HOST")):
        return GcpMetadataSource()
    raise IdentityError(
        "could not detect the runtime; set JFROG_OIDC_SOURCE to gcp, azure, ado or file"
    )
