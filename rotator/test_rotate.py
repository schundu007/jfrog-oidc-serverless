"""Rotator tests against a fake JFrog, metadata server, Secret Manager, AR, Key Vault and ARM.

Run: PYTHONPATH=../client/src python -m unittest test_rotate -v
"""

import base64
import importlib
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


def _jwt(claims):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class Fake(BaseHTTPRequestHandler):
    calls: list = []
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        Fake.calls.append(("GET", u.path, q))
        if u.path.endswith("/default/identity"):
            return self._send(200, _jwt({"sub": "42", "aud": q["audience"]}), "text/plain")
        if u.path.endswith("/default/token"):
            return self._send(200, {"access_token": "gcp-api-token", "expires_in": 3599})
        if u.path.startswith("/sm/projects/p/secrets/jfrog-upstream/versions"):
            versions = [{"name": f"projects/123/secrets/jfrog-upstream/versions/{n}", "state": "ENABLED"} for n in Fake.state["enabled"]]
            return self._send(200, {"versions": versions})
        self._send(404, {"error": u.path})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        body = self._body()
        Fake.calls.append(("POST", u.path, body))
        if u.path == "/access/api/v1/oidc/token":
            return self._send(200, {"access_token": _jwt({"sub": "jfac@x/users/puller"}), "expires_in": Fake.state["ttl"], "username": "puller"})
        if u.path.endswith(":addVersion"):
            n = max(Fake.state["enabled"]) + 1
            Fake.state["enabled"].append(n)
            return self._send(200, {"name": f"projects/123/secrets/jfrog-upstream/versions/{n}"})
        if u.path.endswith(":destroy"):
            return self._send(200, {})
        self._send(404, {"error": u.path})

    def do_PATCH(self):
        u = urllib.parse.urlsplit(self.path)
        if Fake.state.get("patch_fail_codes"):
            self._body()
            return self._send(Fake.state["patch_fail_codes"].pop(0), {"error": "busy"})
        Fake.calls.append(("PATCH", u.path, {"query": dict(urllib.parse.parse_qsl(u.query)), "body": self._body()}))
        self._send(200, {})

    def do_PUT(self):
        u = urllib.parse.urlsplit(self.path)
        body = self._body()
        Fake.calls.append(("PUT", u.path, body))
        self._send(200, {"id": f"https://vault{u.path}/abc123", "value": "redacted"})


class RotatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.calls = []
        Fake.state = {"enabled": [1, 2, 3], "ttl": 14400}

    def run_rotator(self, env, **main_kwargs):
        common = {
            "JFROG_URL": self.base,
            "JFROG_OIDC_ALLOW_HTTP": "1",
            "SECRET_MANAGER_API": f"{self.base}/sm",
            "ARTIFACT_REGISTRY_API": f"{self.base}/ar",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        with mock.patch.dict(os.environ, {**common, **env}, clear=True):
            sys.modules.pop("rotate", None)
            rotate = importlib.import_module("rotate")
            if main_kwargs:
                return rotate.main(**main_kwargs)
            with mock.patch.object(rotate.time, "sleep", lambda s: None):
                return rotate.main()

    def test_gcp_rotation(self):
        rc = self.run_rotator({
            "ROTATOR_TARGET": "gcp",
            "GCE_METADATA_HOST": self.base,
            "JFROG_OIDC_PROVIDER": "gcp-cloudrun",
            "JFROG_OIDC_AUDIENCE": "jfrog-gcp",
            "GCP_SECRET": "projects/p/secrets/jfrog-upstream",
            "AR_REPOSITORIES": "projects/p/locations/us/repositories/jfrog-docker, projects/p/locations/us/repositories/jfrog-pypi",
        })
        self.assertEqual(rc, 0)
        exchange = [c for c in Fake.calls if c[1] == "/access/api/v1/oidc/token"][0][2]
        self.assertEqual(exchange["provider_name"], "gcp-cloudrun")
        added = [c for c in Fake.calls if c[1].endswith(":addVersion")][0][2]
        self.assertTrue(base64.b64decode(added["payload"]["data"]).decode().count(".") == 2)
        patches = [c for c in Fake.calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 2)
        creds = patches[0][2]["body"]["remoteRepositoryConfig"]["upstreamCredentials"]["usernamePasswordCredentials"]
        self.assertEqual(creds, {"username": "puller", "passwordSecretVersion": "projects/123/secrets/jfrog-upstream/versions/4"})
        self.assertEqual(
            patches[0][2]["query"]["updateMask"],
            "remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.username,"
            "remoteRepositoryConfig.upstreamCredentials.usernamePasswordCredentials.passwordSecretVersion",
        )
        destroyed = sorted(c[1].rsplit("/", 1)[1] for c in Fake.calls if c[1].endswith(":destroy"))
        self.assertEqual(destroyed, ["2:destroy"])  # keeps 4 and 3; never the bootstrap v1

    def gcp_env(self):
        return {
            "ROTATOR_TARGET": "gcp",
            "GCE_METADATA_HOST": self.base,
            "JFROG_OIDC_PROVIDER": "gcp-cloudrun",
            "JFROG_OIDC_AUDIENCE": "jfrog-gcp",
            "GCP_SECRET": "projects/p/secrets/jfrog-upstream",
            "AR_REPOSITORIES": "projects/p/locations/us/repositories/jfrog-docker",
        }

    def test_injected_clock_drives_ttl_check(self):
        # A virtual clock far from wall time: the TTL check must use it, or the
        # token would look long expired (or far too long-lived).
        t = [1_000_000.0]
        rc = self.run_rotator(self.gcp_env(), clock=lambda: t[0], sleep=lambda s: None)
        self.assertEqual(rc, 0)

    def test_injected_clock_rejects_short_ttl(self):
        Fake.state["ttl"] = 600
        rc = self.run_rotator(self.gcp_env(), clock=lambda: 1_000_000.0, sleep=lambda s: None)
        self.assertEqual(rc, 1)

    def test_injected_sleep_used_for_http_retries(self):
        Fake.state["patch_fail_codes"] = [503, 503]
        slept = []
        rc = self.run_rotator(self.gcp_env(), clock=lambda: 1_000_000.0, sleep=slept.append)
        self.assertEqual(rc, 0)
        self.assertEqual(slept, [2, 4])

    def test_gcp_rejects_short_ttl(self):
        Fake.state["ttl"] = 600
        rc = self.run_rotator({
            "ROTATOR_TARGET": "gcp",
            "GCE_METADATA_HOST": self.base,
            "JFROG_OIDC_PROVIDER": "gcp-cloudrun",
            "JFROG_OIDC_AUDIENCE": "jfrog-gcp",
            "GCP_SECRET": "projects/p/secrets/jfrog-upstream",
            "AR_REPOSITORIES": "projects/p/locations/us/repositories/jfrog-docker",
        })
        self.assertEqual(rc, 1)
        self.assertFalse([c for c in Fake.calls if c[1].endswith(":addVersion")])

    def gcp_env(self, **extra):
        return {"ROTATOR_TARGET": "gcp", "GCE_METADATA_HOST": self.base,
                "JFROG_OIDC_PROVIDER": "gcp-cloudrun", "JFROG_OIDC_AUDIENCE": "jfrog",
                "GCP_SECRET": "projects/p/secrets/jfrog-upstream",
                "AR_REPOSITORIES": "projects/p/locations/us/repositories/jfrog-docker", **extra}

    def test_azure_target_refused(self):
        with self.assertRaises(SystemExit):
            self.run_rotator({**self.gcp_env(), "ROTATOR_TARGET": "azure"})

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_rotator(self.gcp_env(DRY_RUN="1")), 0)
        self.assertFalse([c for c in Fake.calls if c[1].endswith(":addVersion") or c[0] == "PATCH"])

if __name__ == "__main__":
    unittest.main()
