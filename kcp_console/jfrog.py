"""Artifactory and Access over HTTPS (spec §1). Reads use JFROG_ACCESS_TOKEN from the
environment; writes exist only as functions the runner calls after an approval.

The token is read at call time, sent only as an Authorization header to the
configured host (redirects are never followed), and never logged, stored or returned.
"""

from __future__ import annotations

import json
import os
import urllib.parse

from . import logic

TOKEN_ENV = "JFROG_ACCESS_TOKEN"


class Jfrog:
    def __init__(self, shell, host: str, env=None):
        self.sh, self.host = shell, host
        self.env = os.environ if env is None else env

    @property
    def token_present(self) -> bool:
        return bool(self.env.get(TOKEN_ENV))

    def _headers(self, extra=None):
        h = dict(extra or {})
        tok = self.env.get(TOKEN_ENV)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
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
