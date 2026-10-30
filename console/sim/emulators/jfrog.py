"""Emulated JFrog Platform: OIDC integrations, identity mappings, token minting,
Artifactory reads and Docker registry auth.

Matching follows JFrog's documented rules: the subject token must be signed by
the integration's issuer and carry its audience; every claim in a mapping must
equal the token's claim; the lowest priority number among matches wins; no
match is 401. Issued access tokens are JWTs with sub=jfac@sim/users/<username>
and exp = now + expires_in.
"""

from __future__ import annotations

import base64
import itertools

from . import jwt
from .server import Request

SERVICES = ("access", "artifactory", "registry")


def _err(status: int, message: str):
    return status, {"errors": [{"status": status, "message": message}]}, {}


class JFrogEmulator:
    def __init__(self, clock, trust: dict, key: bytes = b"jfrog-sim-signing-key"):
        self.clock = clock
        self.trust = dict(trust)          # issuer -> HS256 key
        self.key = key
        self.integrations: dict = {}      # name -> {issuer, audience}
        self.mappings: list = []          # dicts, see add_mapping
        self.grants: dict = {}            # group -> set(repo keys)
        self.down: set = set()            # services currently returning 503
        self.minted: list = []            # {jti, username, mapping, iat, exp}
        self._jti = itertools.count(1)

    # -- configuration (engine side) -----------------------------------------

    def add_integration(self, name: str, issuer: str, audience: str) -> None:
        self.integrations[name] = {"issuer": issuer, "audience": audience}

    def add_mapping(self, name, integration, claims, priority, username, expires_in, groups) -> None:
        self.mappings.append({
            "name": name, "integration": integration, "claims": dict(claims), "priority": int(priority),
            "username": username, "expires_in": int(expires_in), "groups": list(groups), "revoked": False,
        })

    def revoke_mapping(self, name: str, revoked: bool = True) -> None:
        for m in self.mappings:
            if m["name"] == name:
                m["revoked"] = revoked

    def mapping(self, name: str) -> dict:
        return next(m for m in self.mappings if m["name"] == name)

    def grant(self, group: str, repos) -> None:
        self.grants.setdefault(group, set()).update(repos)

    def set_down(self, services) -> None:
        self.down = set(services)

    # -- HTTP ----------------------------------------------------------------

    def handle(self, req: Request):
        p = req.path
        if p.startswith("/access/"):
            if "access" in self.down:
                return _err(503, "JFrog Access is unavailable")
            if p == "/access/api/v1/system/ping":
                return 200, "OK", {}
            if p == "/access/api/v1/oidc/token" and req.method == "POST":
                return self._exchange(req.json())
        elif p.startswith("/artifactory/"):
            if "artifactory" in self.down:
                return _err(503, "Artifactory is unavailable")
            return self._artifactory(req)
        elif p == "/v2" or p.startswith("/v2/"):
            if "registry" in self.down:
                return _err(503, "registry is unavailable")
            return self._registry(req)
        return _err(404, f"not found: {p}")

    def _exchange(self, body: dict):
        now = self.clock.now()
        name = body.get("provider_name", "")
        integ = self.integrations.get(name)
        if integ is None:
            return _err(401, f"OIDC provider {name!r} not found")
        subject = body.get("subject_token", "")
        try:
            claims = jwt.claims(subject)
        except jwt.JwtError:
            return _err(401, "subject token is not a JWT")
        iss = claims.get("iss", "")
        if iss != integ["issuer"]:
            return _err(401, f"issuer mismatch: token iss {iss} but provider {name!r} expects {integ['issuer']}")
        key = self.trust.get(iss)
        try:
            if key is None:
                raise jwt.JwtError(f"no keys for issuer {iss}")
            jwt.decode(subject, key)
        except jwt.JwtError as exc:
            return _err(401, f"subject token signature invalid: {exc}")
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if integ["audience"] not in auds:
            return _err(401, f"audience mismatch: token aud {aud} but provider {name!r} expects {integ['audience']}")
        if claims.get("exp", 0) <= now:
            return _err(401, "subject token expired")
        if claims.get("nbf", 0) > now + 60:
            return _err(401, "subject token not yet valid")

        matches = [
            m for m in self.mappings
            if m["integration"] == name and not m["revoked"]
            and all(str(claims.get(k)) == str(v) for k, v in m["claims"].items())
        ]
        if not matches:
            shown = {k: claims[k] for k in ("sub", "oid", "tid") if k in claims}
            return _err(401, f"no identity mapping matched claims {shown} for provider {name!r}")
        m = min(matches, key=lambda x: x["priority"])
        jti = next(self._jti)
        exp = now + m["expires_in"]
        access = jwt.encode({
            "iss": "jfrog@sim", "sub": f"jfac@sim/users/{m['username']}", "aud": "*@*",
            "scp": "applied-permissions/groups:" + ",".join(m["groups"]),
            "iat": int(now), "exp": exp, "jti": f"sim-{jti}",
        }, self.key)
        self.minted.append({"jti": jti, "username": m["username"], "mapping": m["name"], "iat": now, "exp": exp})
        return 200, {
            "access_token": access, "token_type": "Bearer", "expires_in": m["expires_in"],
            "username": m["username"], "scope": "applied-permissions/groups:" + ",".join(m["groups"]),
        }, {}

    def _access_claims(self, token: str):
        """Claims of a valid, unexpired access token, or an error message."""
        try:
            c = jwt.decode(token, self.key)
        except jwt.JwtError:
            return None, "bad credentials"
        if c.get("exp", 0) <= self.clock.now():
            return None, "token expired"
        return c, None

    def _allowed_repos(self, claims: dict) -> set:
        scope = claims.get("scp", "")
        groups = scope.split("groups:", 1)[1].split(",") if "groups:" in scope else []
        repos = set()
        for g in groups:
            repos |= self.grants.get(g, set())
        return repos

    def _artifactory(self, req: Request):
        token = req.bearer()
        if not token:
            return _err(401, "authentication required")
        claims, why = self._access_claims(token)
        if claims is None:
            return _err(401, why)
        rest = req.path[len("/artifactory/"):]
        storage = rest.startswith("api/storage/")
        if storage:
            rest = rest[len("api/storage/"):]
        repo = rest.split("/", 1)[0]
        if repo not in self._allowed_repos(claims):
            return _err(403, f"user lacks read permission on {repo}")
        if storage:
            return 200, {"repo": repo, "path": "/" + rest.split("/", 1)[-1],
                         "children": [{"uri": "/ping.txt", "folder": False}]}, {}
        return 200, b"pong\n", {}

    def _registry(self, req: Request):
        auth = req.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return 401, {"errors": [{"code": "UNAUTHORIZED", "message": "authentication required"}]}, {
                "WWW-Authenticate": 'Basic realm="sim"'}
        try:
            user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except ValueError:
            return _err(401, "bad basic auth")
        claims, why = self._access_claims(password)
        if claims is None:
            return _err(401, why)
        if claims.get("sub", "").rsplit("/users/", 1)[-1] != user:
            return _err(401, f"username {user!r} does not match the token")
        return 200, {"schemaVersion": 2}, {}
