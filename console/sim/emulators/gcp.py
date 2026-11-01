"""Emulated GCP: the metadata server's identity and access tokens, per workload.

Path (under the /gcp prefix):
    /md/<workload>/computeMetadata/v1/instance/service-accounts/default/{identity,token}

Point GCE_METADATA_HOST at <host>/gcp/md/<workload> to give a workload its
own service account.
"""

from __future__ import annotations

import itertools
import re

from . import jwt
from .server import Request

GOOGLE_ISSUER = "https://accounts.google.com"

_MD = re.compile(r"^/md/([^/]+)/computeMetadata/v1/instance/service-accounts/default/(identity|token)$")


class GcpEmulator:
    def __init__(self, clock, key: bytes, project_id: str = "sim-project", project_number: str = "123456789012"):
        self.clock = clock
        self.key = key
        self.project_id = project_id
        self.project_number = project_number
        self.accounts: dict = {}       # workload -> {email, unique_id, down}
        self._n = itertools.count(1)

    # -- configuration -------------------------------------------------------

    def add_account(self, workload: str, email: str, unique_id: str) -> None:
        self.accounts[workload] = {"email": email, "unique_id": str(unique_id), "down": False}

    def recreate_account(self, workload: str, new_unique_id: str) -> None:
        """Delete and recreate the SA with the same email: a new uniqueId."""
        self.accounts[workload]["unique_id"] = str(new_unique_id)

    def set_identity_down(self, workload: str, down: bool) -> None:
        self.accounts[workload]["down"] = down

    # -- HTTP ----------------------------------------------------------------

    def handle(self, req: Request):
        m = _MD.match(req.path)
        if m:
            return self._metadata(req, m.group(1), m.group(2))
        return 404, {"error": {"code": 404, "message": f"not found: {req.path}"}}, {}

    def _metadata(self, req: Request, workload: str, what: str):
        if req.headers.get("metadata-flavor") != "Google":
            return 403, "Missing Metadata-Flavor:Google header", {}
        acct = self.accounts.get(workload)
        if acct is None:
            return 404, f"no service account for {workload}", {}
        if acct["down"]:
            return 503, "metadata server unavailable", {}
        now = self.clock.now()
        if what == "token":
            tok = f"gcp-at-{workload}-{next(self._n):06d}"
            return 200, {"access_token": tok, "expires_in": 3599, "token_type": "Bearer"}, {}
        audience = req.query.get("audience")
        if not audience:
            return 400, "audience is required", {}
        claims = {"iss": GOOGLE_ISSUER, "aud": audience, "sub": acct["unique_id"], "azp": acct["unique_id"],
                  "iat": int(now), "exp": now + 3600}
        if req.query.get("format") == "full":
            claims.update(email=acct["email"], email_verified=True)
        return 200, jwt.encode(claims, self.key), {}
