"""Emulated Azure: managed identity endpoint per workload, Entra token
versions and the app-role gate.

Paths (under the /azure prefix):
    /mi/<workload>?resource=&client_id=       IDENTITY_ENDPOINT (needs X-IDENTITY-HEADER)
"""

from __future__ import annotations


from . import jwt
from .server import Request



def _err(status, code, message):
    return status, {"error": {"code": code, "message": message}}, {}


class AzureEmulator:
    def __init__(self, clock, key: bytes, tenant_id: str):
        self.clock = clock
        self.key = key
        self.tenant_id = tenant_id
        self.workloads: dict = {}   # name -> {header, identities{client_id: principal_id}, down}
        self.app = {"app_id": "", "version": 2, "role_gate": False, "assigned": set()}

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

    # -- HTTP ----------------------------------------------------------------

    def handle(self, req: Request):
        p = req.path
        if p.startswith("/mi/"):
            return self._mi(req, p[4:].strip("/"))
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
