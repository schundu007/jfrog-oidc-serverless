"""Artifactory and Access over HTTPS (spec §1). Writes exist only as functions the runner
calls after an approval.

No JFrog token is stored anywhere. The console mints a short-lived one by OIDC exchange
(OperatorToken): gcloud prints an ID token for the operator service account
(--impersonate-service-account, audience = the JFrog provider's), and JFrog's
/access/api/v1/oidc/token swaps it for an access token. The token lives in memory until
shortly before it expires, is sent only as an Authorization header to the configured
host (redirects are never followed), and is never logged, stored or returned.

The identity (configuration, not secrets) is the ado-trust stack's `console_operator_env` output,
read from its state: the JFrog provider that trusts Google ID tokens, its audience, and the operator
service account the identity mapping matches. Nothing to set before starting the console.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.parse

from . import logic

IDENTITY_KEYS = ("JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SERVICE_ACCOUNT")
EXCHANGE_PATH = "/access/api/v1/oidc/token"
REFRESH_BEFORE = 120  # seconds: mint a new token this long before the old one expires
ADO_TRUST_STATE = pathlib.Path(__file__).resolve().parent.parent / "terraform/stacks/ado-trust/terraform.tfstate"


def operator_identity(state_file=ADO_TRUST_STATE) -> dict:
    """ado-trust's console_operator_env output, or empty values when the stack has none."""
    try:
        out = json.loads(pathlib.Path(state_file).read_text())["outputs"]["console_operator_env"]["value"] or {}
    except (OSError, ValueError, KeyError, TypeError):
        out = {}
    return {k: str(out.get(k) or "") for k in IDENTITY_KEYS}


class TokenError(RuntimeError):
    pass


class OperatorToken:
    """Short-lived JFrog tokens by OIDC exchange, one cached per host in memory only."""

    def __init__(self, clock=time.time, state_file=ADO_TRUST_STATE):
        self.clock, self.state_file = clock, state_file
        self._identity = None
        self._cache = {}  # host -> (token, expires_at)
        self._lock = threading.Lock()

    def _load(self):
        """Read the identity from ado-trust's state on every use: deploying or destroying the stack
        while the console runs changes it. A changed identity drops the cached tokens."""
        ident = tuple(operator_identity(self.state_file).values())
        if ident != self._identity:
            self._identity, self._cache = ident, {}
        self.provider, self.audience, self.service_account = ident

    @property
    def configured(self) -> bool:
        self._load()
        return bool(self.provider and self.audience and self.service_account)

    def get(self, shell, host: str) -> str:
        if not self.configured:
            raise TokenError("no console operator identity: ado-trust is not deployed (or has no console_operators)")
        with self._lock:
            tok, exp = self._cache.get(host, ("", 0.0))
            if tok and exp - REFRESH_BEFORE > self.clock():
                return tok
            idt = shell.run(["gcloud", "auth", "print-identity-token", f"--impersonate-service-account={self.service_account}",
                             f"--audiences={self.audience}", "--include-email"], timeout=60, sensitive=True)
            subject = (idt.stdout or "").strip() if idt.rc == 0 else ""
            if not subject:
                raise TokenError(f"gcloud could not mint an ID token as {self.service_account} "
                                 "(it must exist with roles/iam.serviceAccountTokenCreator for your gcloud login)")
            body = json.dumps({"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                               "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
                               "subject_token": subject, "provider_name": self.provider}).encode()
            res = shell.http("POST", f"https://{host}{EXCHANGE_PATH}", body=body, sensitive=True,
                             headers={"Content-Type": "application/json", "Accept": "application/json"})
            data = res.json() if res.ok else None
            tok = (data or {}).get("access_token") if isinstance(data, dict) else None
            if not tok:
                raise TokenError(f"JFrog OIDC exchange with {self.provider} failed: HTTP {res.status or res.error}")
            self._cache[host] = (tok, self.clock() + float(data.get("expires_in") or 300))
            return tok


class StaticToken:
    """A fixed value: replay mode's placeholder and tests. Never a real token."""

    configured = True

    def __init__(self, value: str):
        self._value = value

    def get(self, shell, host):
        return self._value


class Jfrog:
    def __init__(self, shell, host: str, tokens=None):
        self.sh, self.host = shell, host
        self.tokens = tokens if tokens is not None else OperatorToken()

    @property
    def token_present(self) -> bool:
        """An identity to mint a token with is configured (the exchange itself runs on first use)."""
        return self.tokens.configured

    def token_error(self) -> str:
        """Why no token could be minted, or "" when one was."""
        try:
            self.tokens.get(self.sh, self.host)
            return ""
        except TokenError as exc:
            return str(exc)

    def _headers(self, extra=None):
        h = dict(extra or {})
        if self.tokens.configured:
            try:
                h["Authorization"] = f"Bearer {self.tokens.get(self.sh, self.host)}"
            except TokenError:
                pass  # the call goes out anonymous and its 401/403 says so
        return h

    def _call(self, method, path, body=None, ctype="application/json", host=None, auth=True):
        url = f"https://{host or self.host}{path}"
        data = None if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        headers = self._headers({"Content-Type": ctype} if data else {}) if auth else {}
        res = self.sh.http(method, url, headers=headers, body=data)
        return {"status": res.status, "ok": res.ok, "error": res.error, "body": res.body,
                "json": res.json(), "headers": res.headers, "url": url}

    # -- reads
    def providers(self):
        return self._call("GET", "/access/api/v1/oidc")

    def mappings(self, provider):
        return self._call("GET", f"/access/api/v1/oidc/{urllib.parse.quote(provider)}/identity_mappings")

    def realm_probe(self, host):
        """Anonymous GET /v2/ on a registry host: the 401's realm says which repo the host routes to."""
        r = self._call("GET", "/v2/", host=host, auth=False)
        www = r["headers"].get("www-authenticate", "")
        return {"host": host, "status": r["status"], "www_authenticate": www, "realm": logic.parse_realm(www)}

    def aql(self, prefix):
        q = 'items.find({"path":{"$match":"%s*"}})' % prefix.replace('"', "")
        return self._call("POST", "/artifactory/api/search/aql", body=q, ctype="text/plain")

    def storage(self, repo, path):
        return self._call("GET", f"/artifactory/api/storage/{urllib.parse.quote(repo)}/{urllib.parse.quote(path)}")

    def groups(self):
        """Token scope: Artifactory-admin without Access group rights answers 403 here."""
        return self._call("GET", "/access/api/v2/groups")

    def remote_probe(self, repo, path="library/alpine/manifests/latest"):
        return self._call("GET", f"/artifactory/api/docker/{urllib.parse.quote(repo)}/v2/{path}")

    def request_log(self):
        """The request log for audit attribution. UNVERIFIED API (spec §12)."""
        return self._call("GET", "/artifactory/api/system/logs/data?id=artifactory-request.log")

    # -- writes (called by the runner after an approval only)
    def create_provider(self, body):
        return self._call("POST", "/access/api/v1/oidc", body=body)

    def create_mapping(self, provider, body):
        return self._call("POST", f"/access/api/v1/oidc/{urllib.parse.quote(provider)}/identity_mappings", body=body)

    def delete_mapping(self, provider, name):
        return self._call("DELETE", f"/access/api/v1/oidc/{urllib.parse.quote(provider)}/identity_mappings/{urllib.parse.quote(name)}")

    def delete_provider(self, provider):
        return self._call("DELETE", f"/access/api/v1/oidc/{urllib.parse.quote(provider)}")

    def promote(self, source, body):
        return self._call("POST", f"/artifactory/api/docker/{urllib.parse.quote(source)}/v2/promote", body=body)
