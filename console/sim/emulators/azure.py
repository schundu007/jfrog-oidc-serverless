"""Emulated Azure: managed identity endpoint per workload, Entra token
versions, the app-role gate, Key Vault secrets, and ARM Key Vault reference
refresh.

Paths (under the /azure prefix):
    /mi/<workload>?resource=&client_id=       IDENTITY_ENDPOINT (needs X-IDENTITY-HEADER)
    /kv/<vault>/secrets/<name>                 KEY_VAULT_URI = <host>/azure/kv/<vault>
    /arm/<resource id>/config/configreferences/appsettings/refresh
"""

from __future__ import annotations

import hashlib
import itertools
import re

from . import jwt
from .server import Request

KV_RESOURCE = "https://vault.azure.net"
ARM_RESOURCE = "https://management.azure.com"

_KV = re.compile(r"^/kv/([^/]+)/secrets/([^/]+)$")
_REFRESH = re.compile(r"^/arm(/subscriptions/.+)/config/configreferences/appsettings/refresh$")


def _err(status, code, message):
    return status, {"error": {"code": code, "message": message}}, {}


class AzureEmulator:
    def __init__(self, clock, key: bytes, tenant_id: str):
        self.clock = clock
        self.key = key
        self.tenant_id = tenant_id
        self.workloads: dict = {}   # name -> {header, identities{client_id: principal_id}, down}
        self.app = {"app_id": "", "version": 2, "role_gate": False, "assigned": set()}
        self.vaults: dict = {}      # (vault, name) -> [versions]
        self.refresh_forbidden = False
        self.refreshes: list = []   # {t, app}
        self._issued: dict = {}     # opaque token -> resource kind
        self._n = itertools.count(1)

    @property
    def v1_issuer(self) -> str:
        return f"https://sts.windows.net/{self.tenant_id}/"

    @property
    def v2_issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    # -- configuration -------------------------------------------------------

    def add_workload(self, name: str, header: str, identities: dict) -> None:
        self.workloads[name] = {"header": header, "identities": dict(identities), "down": False}

    def set_identity_down(self, name: str, down: bool) -> None:
        self.workloads[name]["down"] = down

    def set_app(self, app_id: str, version: int = 2, role_gate: bool = False, assigned=None) -> None:
        self.app = {"app_id": app_id, "version": int(version), "role_gate": role_gate, "assigned": set(assigned or ())}

    def add_secret(self, vault: str, name: str, placeholder: str) -> None:
        self.vaults[(vault, name)] = [self._version(1, placeholder, None)]

    def _version(self, n, value, exp):
        vid = hashlib.sha1(f"{n}:{self.clock.now()}".encode()).hexdigest()[:32]
        return {"n": n, "label": f"v{n}", "id": vid, "value": value, "exp": exp, "enabled": True, "created": self.clock.now()}

    # -- inspection ----------------------------------------------------------

    def latest(self, vault: str, name: str) -> dict:
        return self.vaults[(vault, name)][-1]

    # -- HTTP ----------------------------------------------------------------

    def handle(self, req: Request):
        p = req.path
        if p.startswith("/mi/"):
            return self._mi(req, p[4:].strip("/"))
        m = _KV.match(p)
        if m:
            if self._issued.get(req.bearer()) != "kv":
                return _err(401, "Unauthorized", "AKV10000: Request is missing a Bearer or PoP token.")
            return self._kv(req, m.group(1), m.group(2))
        m = _REFRESH.match(p)
        if m and req.method == "POST":
            if self._issued.get(req.bearer()) != "arm":
                return _err(401, "InvalidAuthenticationToken", "The access token is invalid.")
            if self.refresh_forbidden:
                return _err(403, "AuthorizationFailed",
                            "The client does not have authorization to perform action "
                            "'Microsoft.Web/sites/config/configreferences/appsettings/refresh/action'")
            self.refreshes.append({"t": self.clock.now(), "app": m.group(1)})
            return 200, {}, {}
        return _err(404, "NotFound", f"not found: {p}")

    def _mi(self, req: Request, workload: str):
        w = self.workloads.get(workload)
        if w is None:
            return _err(404, "NotFound", f"no identity endpoint for {workload}")
        if req.headers.get("x-identity-header") != w["header"]:
            return _err(401, "Unauthorized", "X-IDENTITY-HEADER missing or invalid")
        if w["down"]:
            return _err(503, "ServiceUnavailable", "managed identity endpoint unavailable")
        client_id = req.query.get("client_id")
        if not client_id:
            if len(w["identities"]) != 1:
                return _err(400, "invalid_request", "multiple user-assigned identities; specify client_id")
            client_id = next(iter(w["identities"]))
        principal = w["identities"].get(client_id)
        if principal is None:
            return _err(400, "invalid_request", f"identity with client_id {client_id} is not assigned to this resource")

        resource = req.query.get("resource", "").rstrip("/")
        now = self.clock.now()
        if resource in (KV_RESOURCE, ARM_RESOURCE):
            kind = "kv" if resource == KV_RESOURCE else "arm"
            tok = f"az-at-{kind}-{next(self._n):06d}"
            self._issued[tok] = kind
            return 200, {"access_token": tok, "expires_on": str(int(now + 3600)), "resource": resource,
                         "token_type": "Bearer"}, {}

        app_id = self.app["app_id"]
        if resource not in (f"api://{app_id}", app_id):
            return _err(400, "invalid_resource", f"AADSTS500011: The resource principal named {resource} was not found")
        if self.app["role_gate"] and principal not in self.app["assigned"]:
            return 400, {"error": "invalid_grant", "error_description": (
                f"AADSTS501051: Application '{client_id}' is not assigned to a role for the application 'api://{app_id}'.")}, {}
        base = {"oid": principal, "sub": principal, "tid": self.tenant_id, "iat": int(now), "nbf": int(now), "exp": now + 3600}
        if self.app["version"] == 1:
            claims = {**base, "iss": self.v1_issuer, "aud": f"api://{app_id}", "appid": client_id, "ver": "1.0"}
        else:
            claims = {**base, "iss": self.v2_issuer, "aud": app_id, "azp": client_id, "ver": "2.0"}
        return 200, {"access_token": jwt.encode(claims, self.key), "expires_on": str(int(now + 3600)),
                     "resource": resource, "token_type": "Bearer"}, {}

    def _kv(self, req: Request, vault: str, name: str):
        versions = self.vaults.get((vault, name))
        if versions is None:
            return _err(404, "SecretNotFound", f"A secret with name {name} was not found in this key vault.")
        if req.method == "PUT":
            body = req.json()
            attrs = body.get("attributes", {})
            v = self._version(versions[-1]["n"] + 1, body.get("value", ""), attrs.get("exp"))
            v["enabled"] = attrs.get("enabled", True)
            versions.append(v)
            # The real API echoes the value; this one doesn't, so nothing downstream can log it.
            return 200, {"id": f"https://{vault}.vault.azure.net/secrets/{name}/{v['id']}",
                         "attributes": {"enabled": v["enabled"], "exp": v["exp"], "created": int(v["created"])},
                         "contentType": body.get("contentType"), "tags": body.get("tags", {})}, {}
        if req.method == "GET":
            v = versions[-1]
            return 200, {"id": f"https://{vault}.vault.azure.net/secrets/{name}/{v['id']}",
                         "attributes": {"enabled": v["enabled"], "exp": v["exp"]}}, {}
        return _err(405, "MethodNotAllowed", req.method)
