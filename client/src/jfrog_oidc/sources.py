"""Identity sources: obtain a platform-signed OIDC token for the running workload.

Each source returns a raw JWT string signed by the cloud's identity platform.
Nothing here holds a long-lived secret; the token comes from a local endpoint
that only the workload itself can reach.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from . import sigv4
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
    mapped into the step's env) and gets the connection's token: iss
    https://login.microsoftonline.com/<tenant>/v2.0 and sub
    /eid1/c/pub/t/.../sc/<org-id>/<connection-id> (older connections: iss
    https://vstoken.dev.azure.com/<org-id>, sub sc://<org>/<project>/<connection>).
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
        query = urllib.parse.urlencode({"api-version": "7.1-preview.1", "serviceConnectionId": self._connection})
        url = f"{self._uri}{'&' if '?' in self._uri else '?'}{query}"
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={
            "Authorization": f"Bearer {self._job_token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise IdentityError(f"Azure DevOps OIDC endpoint returned HTTP {exc.code}: {_error_message(exc)}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise IdentityError(f"Azure DevOps OIDC endpoint unreachable: {exc}") from None
        try:
            token = json.loads(raw)["oidcToken"]
        except (ValueError, KeyError, TypeError):
            raise IdentityError("Azure DevOps OIDC response missing oidcToken") from None
        if not isinstance(token, str) or token.count(".") != 2:
            raise IdentityError("Azure DevOps did not return a JWT")
        return token


class AwsStsSource:
    """JWT from AWS IAM outbound identity federation (STS GetWebIdentityToken).

    The workload signs the call (SigV4) with its own temporary role credentials:
    Lambda puts them in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
    AWS_SESSION_TOKEN; ECS tasks and App Runner serve them from the container
    credentials endpoint (AWS_CONTAINER_CREDENTIALS_RELATIVE_URI on 169.254.170.2,
    or AWS_CONTAINER_CREDENTIALS_FULL_URI with AWS_CONTAINER_AUTHORIZATION_TOKEN[_FILE]).
    STS answers with a JWT whose iss is the account's issuer
    (https://<id>.tokens.sts.global.api.aws) and whose sub is the role ARN.

    The API is not served by the global STS endpoint: the regional one comes from
    JFROG_OIDC_AWS_REGION / AWS_REGION / AWS_DEFAULT_REGION, or AWS_ENDPOINT_URL_STS (the SDKs' standard
    override). The account must have outbound federation enabled
    (aws iam enable-outbound-web-identity-federation).
    """

    name = "aws"
    VERSION = "2011-06-15"
    ECS_HOST = "http://169.254.170.2"
    SIGNING_ALGORITHM = "RS256"

    def __init__(self, region: Optional[str] = None, duration_seconds: int = 900,
                 timeout: float = DEFAULT_TIMEOUT, clock=time.time):
        env = os.environ
        # JFROG_OIDC_AWS_REGION first: Lambda reserves AWS_REGION, and the console sets this one everywhere.
        self._region = (region or env.get("JFROG_OIDC_AWS_REGION") or env.get("AWS_REGION")
                        or env.get("AWS_DEFAULT_REGION") or "")
        self._endpoint = (env.get("AWS_ENDPOINT_URL_STS") or env.get("AWS_ENDPOINT_URL") or "").rstrip("/")
        if not self._region:
            raise IdentityError("AwsStsSource needs a region (AWS_REGION): GetWebIdentityToken is regional only")
        if not self._endpoint:
            self._endpoint = f"https://sts.{self._region}.amazonaws.com"
        if not self._endpoint.startswith("https://") and env.get("JFROG_OIDC_ALLOW_HTTP") != "1":
            raise IdentityError("the STS endpoint must be https")
        if not 60 <= int(duration_seconds) <= 3600:
            raise IdentityError("duration_seconds must be between 60 and 3600")
        self._duration = int(duration_seconds)
        self._timeout = timeout
        self._clock = clock

    # -- credentials ---------------------------------------------------------------

    def _credentials(self) -> dict:
        env = os.environ
        if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            return {"AccessKeyId": env["AWS_ACCESS_KEY_ID"], "SecretAccessKey": env["AWS_SECRET_ACCESS_KEY"],
                    "Token": env.get("AWS_SESSION_TOKEN") or None}
        rel, full = env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"), env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        headers = {}
        if rel:
            url = self.ECS_HOST + rel
        elif full:
            host = urllib.parse.urlsplit(full).hostname or ""
            if host not in ("127.0.0.1", "localhost", "::1", "169.254.170.2", "169.254.170.23", "fd00:ec2::23"):
                raise IdentityError("AWS_CONTAINER_CREDENTIALS_FULL_URI must point at a local credentials endpoint")
            url = full
            token_file = env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
            if token_file:
                try:
                    with open(token_file, encoding="utf-8") as fh:
                        headers["Authorization"] = fh.read().strip()
                except OSError as exc:
                    raise IdentityError(f"cannot read AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: {exc}") from None
            elif env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN"):
                headers["Authorization"] = env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
        else:
            raise IdentityError("no AWS credentials: expected Lambda's AWS_ACCESS_KEY_ID or a container "
                                "credentials endpoint (ECS task role, App Runner instance role)")
        try:
            body = json.loads(_get(url, headers, self._timeout))
        except ValueError:
            raise IdentityError("container credentials endpoint returned non-JSON") from None
        if not (body.get("AccessKeyId") and body.get("SecretAccessKey")):
            raise IdentityError("container credentials response missing AccessKeyId/SecretAccessKey")
        return {"AccessKeyId": body["AccessKeyId"], "SecretAccessKey": body["SecretAccessKey"],
                "Token": body.get("Token") or None}

    # -- token ---------------------------------------------------------------------

    def get_token(self, audience: str) -> str:
        if not audience:
            raise IdentityError("AwsStsSource needs an audience (JFROG_OIDC_AUDIENCE)")
        creds = self._credentials()
        form = urllib.parse.urlencode({"Action": "GetWebIdentityToken", "Version": self.VERSION,
                                       "Audience.member.1": audience, "SigningAlgorithm": self.SIGNING_ALGORITHM,
                                       "DurationSeconds": str(self._duration)}).encode()
        url = self._endpoint + "/"
        headers = sigv4.sign("POST", url, {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, form,
                             access_key=creds["AccessKeyId"], secret_key=creds["SecretAccessKey"],
                             session_token=creds["Token"], region=self._region, service="sts", now=self._clock())
        del creds
        req = urllib.request.Request(url, data=form, method="POST", headers=headers)
        # The regional STS endpoint may sit behind the operator's proxy; a local (http) one never does.
        opener = _NO_PROXY_OPENER if url.startswith("http://") else urllib.request.build_opener()
        try:
            with opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            code, message = _sts_error(exc)
            hint = (": enable it with aws iam enable-outbound-web-identity-federation"
                    if code == "OutboundWebIdentityFederationDisabledException" else "")
            raise IdentityError(f"STS GetWebIdentityToken returned HTTP {exc.code} {code}: {message}{hint}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise IdentityError(f"STS endpoint unreachable: {exc}") from None
        token = _xml_text(raw, "WebIdentityToken")
        if not token or token.count(".") != 2:
            raise IdentityError("STS did not return a WebIdentityToken JWT")
        return token


def _xml_text(raw: bytes, tag: str) -> str:
    """Text of the first element named tag (any namespace) in an STS XML response."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag:
            return (el.text or "").strip()
    return ""


def _sts_error(exc: urllib.error.HTTPError) -> tuple:
    """(Code, Message) from an STS XML error body; never echoes request headers."""
    try:
        raw = exc.read()[:4000]
    except (AttributeError, OSError):
        raw = b""
    return _xml_text(raw, "Code") or "error", _xml_text(raw, "Message")[:300]


def _error_message(exc: urllib.error.HTTPError) -> str:
    """The "message" of an Azure DevOps error body (never echoes request headers)."""
    try:
        text = exc.read().decode("utf-8-sig", "replace")
    except (AttributeError, OSError):
        return ""
    try:
        msg = json.loads(text).get("message", "")
    except (ValueError, AttributeError):
        msg = " ".join(text.split())
    return str(msg)[:300]


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
    if choice == "aws":
        return AwsStsSource()
    if choice != "auto":
        raise IdentityError(f"unknown JFROG_OIDC_SOURCE={choice!r} (use auto, gcp, azure, aws, ado or file)")

    if os.environ.get("JFROG_OIDC_TOKEN_FILE"):
        return FileTokenSource()
    # Azure DevOps agents often run on Azure VMs: check the pipeline first.
    if os.environ.get("SYSTEM_OIDCREQUESTURI"):
        return AzureDevOpsSource()
    if os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
        return AzureManagedIdentitySource()
    # Lambda sets AWS_LAMBDA_FUNCTION_NAME; ECS tasks and App Runner serve a container credentials endpoint.
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") \
            or os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        return AwsStsSource()
    # Cloud Run services/functions set K_SERVICE; jobs set CLOUD_RUN_JOB.
    if any(os.environ.get(v) for v in ("K_SERVICE", "CLOUD_RUN_JOB", "FUNCTION_TARGET", "GCE_METADATA_HOST")):
        return GcpMetadataSource()
    raise IdentityError(
        "could not detect the runtime; set JFROG_OIDC_SOURCE to gcp, azure, aws, ado or file"
    )
