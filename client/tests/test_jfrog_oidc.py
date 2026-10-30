"""End-to-end tests against a local fake of the metadata/identity endpoints and JFrog.

Run: PYTHONPATH=src python -m unittest discover -s tests -v
"""

import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from jfrog_oidc import (
    AzureDevOpsSource,
    AzureManagedIdentitySource,
    ExchangeError,
    GcpMetadataSource,
    IdentityError,
    JFrogTokenProvider,
    download,
)
from jfrog_oidc.sources import detect_source


def _jwt(claims: dict) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'RS256', 'typ': 'JWT'})}.{enc(claims)}.sig"


class FakeCloud(BaseHTTPRequestHandler):
    """Serves GCP metadata, Azure IDENTITY_ENDPOINT and the JFrog exchange."""

    state: dict = {}

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        st = self.state
        if url.path.endswith("/service-accounts/default/identity"):
            if self.headers.get("Metadata-Flavor") != "Google":
                return self._send(403, {"error": "missing Metadata-Flavor"})
            st["gcp_audiences"].append(q.get("audience"))
            assert q.get("format") == "full"
            return self._send(200, _jwt({"iss": "https://accounts.google.com", "aud": q["audience"], "sub": "1234567890"}), "text/plain")
        if url.path == "/msi/token":
            if self.headers.get("X-IDENTITY-HEADER") != "secret-header":
                return self._send(401, {"error": "bad identity header"})
            st["azure_requests"].append(q)
            return self._send(200, {"access_token": _jwt({"oid": "mi-oid", "aud": "app-id"}), "expires_on": "9999999999"})
        if url.path.startswith("/artifactory/generic-local/"):
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {st['current_access']}":
                return self._send(401, {"errors": [{"status": 401}]})
            return self._send(200, b"artifact-bytes", "application/octet-stream")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        st = self.state
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        url = urllib.parse.urlsplit(self.path)
        if url.path.endswith("/oidctoken"):  # Azure DevOps SYSTEM_OIDCREQUESTURI
            if self.headers.get("Authorization") != "Bearer ado-job-token":
                return self._send(401, {"message": "bad job token"})
            if urllib.parse.parse_qs(url.query).get("api-version") != ["7.1-preview.1"]:
                return self._send(400, {"message": "The requested REST API version is in preview."})
            st.setdefault("ado_requests", []).append(dict(urllib.parse.parse_qsl(url.query)))
            sub = "sc://acme/payments/jfrog-oidc"
            return self._send(200, {"oidcToken": _jwt({"iss": "https://vstoken.dev.azure.com/org-guid", "sub": sub,
                                                       "aud": "api://AzureADTokenExchange"})})
        if self.path == "/access/api/v1/oidc/token":
            st["exchanges"].append(body)
            if st["fail_codes"]:
                return self._send(st["fail_codes"].pop(0), {"errors": [{"message": "boom"}]})
            assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
            assert body["subject_token_type"] == "urn:ietf:params:oauth:token-type:id_token"
            if body["provider_name"] != st["provider"]:
                return self._send(401, {"errors": [{"message": "unknown provider"}]})
            n = len(st["exchanges"])
            access = _jwt({"sub": f"jfac@01abc/users/{st['username']}", "exp": 4102444800, "n": n})
            st["current_access"] = access
            resp = {"access_token": access, "expires_in": st["expires_in"], "token_type": "Bearer"}
            if st.get("include_username", True):
                resp["username"] = st["username"]
            return self._send(200, resp)
        self._send(404, {"error": "not found"})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeCloud.state = {
            "gcp_audiences": [],
            "azure_requests": [],
            "exchanges": [],
            "fail_codes": [],
            "provider": "gcp-cloudrun",
            "username": "cr-orders",
            "expires_in": 3600,
            "current_access": None,
        }
        self.env = mock.patch.dict(os.environ, {"JFROG_OIDC_ALLOW_HTTP": "1"}, clear=False)
        self.env.start()
        self.clock = Clock()

    def tearDown(self):
        self.env.stop()

    def gcp_provider(self, **kw):
        return JFrogTokenProvider(
            jfrog_url=self.base,
            provider_name="gcp-cloudrun",
            audience="jfrog-gcp",
            source=GcpMetadataSource(metadata_host=self.base),
            clock=self.clock,
            sleep=lambda s: None,
            **kw,
        )


class GcpFlowTests(BaseCase):
    def test_exchange_and_cache(self):
        p = self.gcp_provider()
        first = p.token()
        second = p.token()
        self.assertIs(first, second)
        self.assertEqual(first.username, "cr-orders")
        self.assertEqual(len(FakeCloud.state["exchanges"]), 1)
        self.assertEqual(FakeCloud.state["gcp_audiences"], ["jfrog-gcp"])
        self.assertEqual(FakeCloud.state["exchanges"][0]["provider_name"], "gcp-cloudrun")
        self.assertNotIn("project_key", FakeCloud.state["exchanges"][0])

    def test_refresh_near_expiry(self):
        p = self.gcp_provider(refresh_skew=300)
        first = p.token()
        self.clock.t += 3600 - 299  # inside the skew window
        second = p.token()
        self.assertNotEqual(first.access_token, second.access_token)
        self.assertEqual(len(FakeCloud.state["exchanges"]), 2)

    def test_short_ttl_clamps_skew(self):
        FakeCloud.state["expires_in"] = 600
        p = self.gcp_provider(refresh_skew=300)
        p.token()
        self.clock.t += 350  # 250s left; skew clamps to 200s, so still cached
        p.token()
        self.assertEqual(len(FakeCloud.state["exchanges"]), 1)

    def test_retry_on_5xx_then_success(self):
        FakeCloud.state["fail_codes"] = [503, 502]
        tok = self.gcp_provider().token()
        self.assertEqual(tok.username, "cr-orders")
        self.assertEqual(len(FakeCloud.state["exchanges"]), 3)

    def test_no_retry_on_401(self):
        FakeCloud.state["provider"] = "something-else"
        with self.assertRaises(ExchangeError) as ctx:
            self.gcp_provider().token()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(FakeCloud.state["exchanges"]), 1)

    def test_serves_cached_token_when_refresh_fails(self):
        p = self.gcp_provider()
        first = p.token()
        self.clock.t += 3600 - 120  # needs refresh, 120s still valid
        FakeCloud.state["fail_codes"] = [500, 500, 500]
        self.assertIs(p.token(), first)

    def test_backs_off_after_failed_refresh(self):
        p = self.gcp_provider()
        p.token()
        self.clock.t += 3600 - 120
        FakeCloud.state["fail_codes"] = [500, 500, 500]
        p.token()
        attempts = len(FakeCloud.state["exchanges"])
        for _ in range(5):
            p.token()  # inside the 30s backoff: no new exchange attempts
        self.assertEqual(len(FakeCloud.state["exchanges"]), attempts)
        self.clock.t += 31
        p.token()  # backoff over: retries and succeeds
        self.assertEqual(len(FakeCloud.state["exchanges"]), attempts + 1)

    def test_valid_token_never_waits_for_lock(self):
        p = self.gcp_provider()
        first = p.token()
        self.clock.t += 3600 - 120
        p._lock.acquire()  # another thread is mid-refresh
        try:
            self.assertIs(p.token(), first)
        finally:
            p._lock.release()

    def test_auth_header_not_forwarded_on_redirect(self):
        from jfrog_oidc.artifactory import _open

        p = self.gcp_provider()
        p.token()
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = mock.MagicMock()
            _open(p, self.base + "/artifactory/generic-local/x")
            req = urlopen.call_args[0][0]
        self.assertIn("Authorization", req.unredirected_hdrs)
        self.assertNotIn("Authorization", req.headers)

    def test_raises_when_cached_token_expired_and_refresh_fails(self):
        p = self.gcp_provider()
        p.token()
        self.clock.t += 3600 + 1
        FakeCloud.state["fail_codes"] = [500, 500, 500]
        with self.assertRaises(ExchangeError):
            p.token()

    def test_username_from_jwt_when_not_returned(self):
        FakeCloud.state["include_username"] = False
        self.assertEqual(self.gcp_provider().token().username, "cr-orders")

    def test_project_key_sent(self):
        self.gcp_provider(project_key="payments").token()
        self.assertEqual(FakeCloud.state["exchanges"][0]["project_key"], "payments")

    def test_token_not_in_repr(self):
        tok = self.gcp_provider().token()
        self.assertNotIn(tok.access_token, repr(tok))

    def test_download_refreshes_on_401(self):
        p = self.gcp_provider()
        p.token()
        FakeCloud.state["current_access"] = "rotated-elsewhere"  # server no longer accepts cached token
        with tempfile.TemporaryDirectory() as d:
            dest = download(p, "generic-local/models/m.bin", os.path.join(d, "m.bin"))
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), b"artifact-bytes")
        self.assertEqual(len(FakeCloud.state["exchanges"]), 2)

    def test_requires_https(self):
        with mock.patch.dict(os.environ, {"JFROG_OIDC_ALLOW_HTTP": ""}):
            with self.assertRaises(ValueError):
                JFrogTokenProvider("http://acme.jfrog.io", "p", "a", source=GcpMetadataSource())

    def test_requires_platform_origin(self):
        # The client appends /access/... itself; a path would double it up
        # (the AKS pilot's https://https://... failure in another form).
        with mock.patch.dict(os.environ, {"JFROG_OIDC_ALLOW_HTTP": ""}):
            for bad in ("https://acme.jfrog.io/artifactory", "https://acme.jfrog.io/access/api/v1/oidc/token",
                        "https://svc:pw@acme.jfrog.io", "https://acme.jfrog.io?x=1"):
                with self.subTest(url=bad), self.assertRaises(ValueError):
                    JFrogTokenProvider(bad, "p", "a", source=GcpMetadataSource())
            ok = JFrogTokenProvider("https://acme.jfrog.io/", "p", "a", source=GcpMetadataSource())
            self.assertEqual(ok.jfrog_url, "https://acme.jfrog.io")


class AzureFlowTests(BaseCase):
    def test_container_apps_identity_endpoint(self):
        FakeCloud.state["provider"] = "azure-entra"
        FakeCloud.state["username"] = "aca-orders"
        env = {
            "IDENTITY_ENDPOINT": f"{self.base}/msi/token",
            "IDENTITY_HEADER": "secret-header",
            "AZURE_CLIENT_ID": "mi-client-id",
        }
        with mock.patch.dict(os.environ, env):
            p = JFrogTokenProvider(
                jfrog_url=self.base,
                provider_name="azure-entra",
                audience="api://app-id",
                source=AzureManagedIdentitySource(),
                clock=self.clock,
                sleep=lambda s: None,
            )
            tok = p.token()
        self.assertEqual(tok.username, "aca-orders")
        req = FakeCloud.state["azure_requests"][0]
        self.assertEqual(req["resource"], "api://app-id")
        self.assertEqual(req["client_id"], "mi-client-id")
        self.assertEqual(req["api-version"], "2019-08-01")


class AzureDevOpsFlowTests(BaseCase):
    def ado_env(self, **extra):
        env = {"SYSTEM_OIDCREQUESTURI": f"{self.base}/acme/proj/_apis/distributedtask/hubs/build/plans/p1/jobs/j1/oidctoken",
               "SYSTEM_ACCESSTOKEN": "ado-job-token"}
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def ado_provider(self, source):
        return JFrogTokenProvider(jfrog_url=self.base, provider_name="ado", audience="api://AzureADTokenExchange",
                                  source=source, clock=self.clock, sleep=lambda s: None)

    def test_pipeline_token_for_service_connection(self):
        FakeCloud.state["provider"] = "ado"
        with self.ado_env(JFROG_OIDC_ADO_SERVICE_CONNECTION_ID="sc-123"):
            tok = self.ado_provider(AzureDevOpsSource()).token()
        self.assertEqual(tok.username, "cr-orders")
        req = FakeCloud.state["ado_requests"][0]
        self.assertEqual(req["serviceConnectionId"], "sc-123")
        self.assertEqual(req["api-version"], "7.1-preview.1")
        self.assertEqual(FakeCloud.state["exchanges"][0]["provider_name"], "ado")

    def test_needs_a_service_connection(self):
        with self.ado_env():
            with self.assertRaises(IdentityError) as ctx:
                AzureDevOpsSource()
        self.assertIn("JFROG_OIDC_ADO_SERVICE_CONNECTION_ID", str(ctx.exception))

    def test_job_token_never_in_errors(self):
        with self.ado_env(SYSTEM_ACCESSTOKEN="wrong-job-token-value"):
            with self.assertRaises(IdentityError) as ctx:
                AzureDevOpsSource(service_connection_id="sc-1").get_token("ignored")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("bad job token", str(ctx.exception))
        self.assertNotIn("wrong-job-token-value", str(ctx.exception))

    def test_refuses_plain_http_unless_allowed(self):
        with self.ado_env(JFROG_OIDC_ALLOW_HTTP="0"):
            with self.assertRaises(IdentityError):
                AzureDevOpsSource(service_connection_id="sc-1").get_token("ignored")


class DetectTests(unittest.TestCase):
    def test_detects_azure_devops(self):
        env = {"SYSTEM_OIDCREQUESTURI": "https://dev.azure.com/x", "SYSTEM_ACCESSTOKEN": "t",
               "JFROG_OIDC_ADO_SERVICE_CONNECTION_ID": "sc"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(detect_source().name, "ado")

    def test_detects_azure(self):
        with mock.patch.dict(os.environ, {"IDENTITY_ENDPOINT": "http://x", "JFROG_OIDC_SOURCE": "auto"}, clear=True):
            self.assertEqual(detect_source().name, "azure")

    def test_detects_cloud_run(self):
        with mock.patch.dict(os.environ, {"K_SERVICE": "orders"}, clear=True):
            self.assertEqual(detect_source().name, "gcp")

    def test_detects_cloud_run_job(self):
        with mock.patch.dict(os.environ, {"CLOUD_RUN_JOB": "rotator"}, clear=True):
            self.assertEqual(detect_source().name, "gcp")

    def test_unknown_runtime_raises(self):
        from jfrog_oidc import IdentityError

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(IdentityError):
                detect_source()


if __name__ == "__main__":
    unittest.main()
