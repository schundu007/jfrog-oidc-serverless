"""Emulated GCP: metadata identity/token per workload, Secret Manager, and
Artifact Registry remote repositories.

Paths (under the /gcp prefix):
    /md/<workload>/computeMetadata/v1/instance/service-accounts/default/{identity,token}
    /sm/v1/projects/<p>/secrets/<s>:addVersion | /versions | /versions/<n>:{access,disable,enable,destroy}
    /ar/v1/projects/<p>/locations/<l>/repositories/<r>   (GET, PATCH with updateMask)

Point GCE_METADATA_HOST at <host>/gcp/md/<workload> to give a workload its
own service account.
"""

from __future__ import annotations

import base64
import itertools
import re

from . import jwt
from .server import Request

GOOGLE_ISSUER = "https://accounts.google.com"
CREDS = "remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials"
AR_MASK_FIELDS = {f"{CREDS}.username": "username", f"{CREDS}.passwordSecretVersion": "passwordSecretVersion",
                  "description": None, "labels": None}

_MD = re.compile(r"^/md/([^/]+)/computeMetadata/v1/instance/service-accounts/default/(identity|token)$")
_SM_ADD = re.compile(r"^/sm/v1/projects/([^/]+)/secrets/([^/:]+):addVersion$")
_SM_LIST = re.compile(r"^/sm/v1/projects/([^/]+)/secrets/([^/:]+)/versions$")
_SM_VER = re.compile(r"^/sm/v1/projects/([^/]+)/secrets/([^/:]+)/versions/(\d+):(access|disable|enable|destroy)$")
_AR = re.compile(r"^/ar/v1/projects/([^/]+)/locations/([^/]+)/repositories/([^/]+)$")


def _err(status, message):
    return status, {"error": {"code": status, "message": message}}, {}


class GcpEmulator:
    def __init__(self, clock, key: bytes, project_id: str = "sim-project", project_number: str = "123456789012"):
        self.clock = clock
        self.key = key
        self.project_id = project_id
        self.project_number = project_number
        self.accounts: dict = {}       # workload -> {email, unique_id, down}
        self.secrets: dict = {}        # secret id -> [version dicts]
        self.repos: dict = {}          # repo name -> {location, username, passwordSecretVersion}
        self._issued: set = set()
        self._n = itertools.count(1)

    # -- configuration -------------------------------------------------------

    def add_account(self, workload: str, email: str, unique_id: str) -> None:
        self.accounts[workload] = {"email": email, "unique_id": str(unique_id), "down": False}

    def recreate_account(self, workload: str, new_unique_id: str) -> None:
        """Delete and recreate the SA with the same email: a new uniqueId."""
        self.accounts[workload]["unique_id"] = str(new_unique_id)

    def set_identity_down(self, workload: str, down: bool) -> None:
        self.accounts[workload]["down"] = down

    def add_secret(self, secret: str, placeholder: str) -> None:
        self.secrets[secret] = [{"n": 1, "data": placeholder.encode(), "state": "ENABLED", "create_time": self.clock.now()}]

    def add_remote_repo(self, location: str, name: str, username: str, version_name: str) -> None:
        self.repos[name] = {"location": location, "username": username, "passwordSecretVersion": version_name}

    # -- inspection (platforms and engine) ------------------------------------

    def version_name(self, secret: str, n: int) -> str:
        return f"projects/{self.project_number}/secrets/{secret}/versions/{n}"

    def repo(self, name: str) -> dict:
        return dict(self.repos[name])

    def version(self, name: str):
        m = re.match(r"^projects/[^/]+/secrets/([^/]+)/versions/(\d+)$", name or "")
        if not m or m.group(1) not in self.secrets:
            return None
        n = int(m.group(2))
        return next((v for v in self.secrets[m.group(1)] if v["n"] == n), None)

    def latest(self, secret: str) -> dict:
        return self.secrets[secret][-1]

    # -- HTTP ----------------------------------------------------------------

    def handle(self, req: Request):
        p = req.path
        m = _MD.match(p)
        if m:
            return self._metadata(req, m.group(1), m.group(2))
        if p.startswith("/sm/") or p.startswith("/ar/"):
            if req.bearer() not in self._issued:
                return _err(401, "Request had invalid authentication credentials.")
        m = _SM_ADD.match(p)
        if m and req.method == "POST":
            return self._add_version(m.group(1), m.group(2), req.json())
        m = _SM_LIST.match(p)
        if m and req.method == "GET":
            return self._list(m.group(1), m.group(2), req.query.get("filter", ""))
        m = _SM_VER.match(p)
        if m:
            return self._version_op(m.group(1), m.group(2), int(m.group(3)), m.group(4))
        m = _AR.match(p)
        if m:
            return self._ar(req, m.group(3))
        return _err(404, f"not found: {p}")

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
            self._issued.add(tok)
            return 200, {"access_token": tok, "expires_in": 3599, "token_type": "Bearer"}, {}
        audience = req.query.get("audience")
        if not audience:
            return 400, "audience is required", {}
        claims = {"iss": GOOGLE_ISSUER, "aud": audience, "sub": acct["unique_id"], "azp": acct["unique_id"],
                  "iat": int(now), "exp": now + 3600}
        if req.query.get("format") == "full":
            claims.update(email=acct["email"], email_verified=True)
        return 200, jwt.encode(claims, self.key), {}

    def _secret(self, project: str, secret: str):
        if project not in (self.project_id, self.project_number):
            return None
        return self.secrets.get(secret)

    def _add_version(self, project, secret, body):
        versions = self._secret(project, secret)
        if versions is None:
            return _err(404, f"Secret [{secret}] not found")
        try:
            data = base64.b64decode(body["payload"]["data"])
        except (KeyError, TypeError, ValueError):
            return _err(400, "payload.data must be base64")
        n = versions[-1]["n"] + 1
        versions.append({"n": n, "data": data, "state": "ENABLED", "create_time": self.clock.now()})
        return 200, {"name": self.version_name(secret, n), "state": "ENABLED"}, {}

    def _list(self, project, secret, flt):
        versions = self._secret(project, secret)
        if versions is None:
            return _err(404, f"Secret [{secret}] not found")
        want = flt.split(":", 1)[1].strip().upper() if flt.startswith("state:") else None
        out = [{"name": self.version_name(secret, v["n"]), "state": v["state"]}
               for v in reversed(versions) if want is None or v["state"] == want]
        return 200, {"versions": out, "totalSize": len(out)}, {}

    def _version_op(self, project, secret, n, op):
        versions = self._secret(project, secret)
        v = next((x for x in versions or [] if x["n"] == n), None)
        if v is None:
            return _err(404, f"Secret Version [{n}] not found")
        if op == "access":
            if v["state"] != "ENABLED":
                return _err(400, f"Secret Version [{n}] is in {v['state']} state")
            return 200, {"name": self.version_name(secret, n), "payload": {"data": base64.b64encode(v["data"]).decode()}}, {}
        if v["state"] == "DESTROYED" and op != "destroy":
            return _err(400, f"Secret Version [{n}] is DESTROYED")
        v["state"] = {"disable": "DISABLED", "enable": "ENABLED", "destroy": "DESTROYED"}[op]
        if op == "destroy":
            v["data"] = b""
        return 200, {"name": self.version_name(secret, n), "state": v["state"]}, {}

    def _ar(self, req: Request, name: str):
        repo = self.repos.get(name)
        if repo is None:
            return _err(404, f"Repository {name} not found")
        if req.method == "GET":
            return 200, self._ar_view(name), {}
        if req.method != "PATCH":
            return _err(405, "method not allowed")
        mask = [f.strip() for f in req.query.get("updateMask", "").split(",") if f.strip()]
        if not mask:
            return _err(400, "update_mask is required")
        bad = [f for f in mask if f not in AR_MASK_FIELDS]
        if bad:
            return _err(400, f"Invalid update_mask path(s): {', '.join(bad)}")
        creds = (req.json().get("remoteRepositoryConfig", {}).get("upstreamCredentials", {})
                 .get("usernamePasswordCredentials", {}))
        for f in mask:
            field = AR_MASK_FIELDS[f]
            if field:
                repo[field] = creds.get(field, "")
        return 200, self._ar_view(name), {}

    def _ar_view(self, name):
        r = self.repos[name]
        return {"name": f"projects/{self.project_id}/locations/{r['location']}/repositories/{name}", "format": "DOCKER",
                "mode": "REMOTE_REPOSITORY",
                "remoteRepositoryConfig": {"upstreamCredentials": {"usernamePasswordCredentials": {
                    "username": r["username"], "passwordSecretVersion": r["passwordSecretVersion"]}}}}
