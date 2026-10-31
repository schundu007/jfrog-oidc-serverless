"""The portal: one origin, two strictly separate sections (Serverless at /serverless, Kubernetes at /k8s)."""

import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path

from console.server import ConsoleServer
from console.store import Store
from kcp_console.server import KcpServer
from kcp_console.store import Store as KcpStore
from portal.server import Portal, legacy_target, section_of

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


class RoutingTests(unittest.TestCase):
    def test_section_of(self):
        self.assertEqual(section_of("/serverless"), ("/serverless", "/"))
        self.assertEqual(section_of("/serverless/api/schema"), ("/serverless", "/api/schema"))
        self.assertEqual(section_of("/k8s/clusters/aks"), ("/k8s", "/clusters/aks"))
        self.assertEqual(section_of("/serverlessx/api"), (None, "/serverlessx/api"))
        self.assertEqual(section_of("/api/configs"), (None, "/api/configs"))

    def test_legacy_target(self):
        self.assertEqual(legacy_target("/gcp/runs"), "/serverless/gcp/runs")
        self.assertEqual(legacy_target("/azure"), "/serverless/azure")
        self.assertIsNone(legacy_target("/clusters/gke/c1/edit"))  # the standalone KCP console is gone
        self.assertIsNone(legacy_target("/api/configs"))
        self.assertIsNone(legacy_target("/static/app.js"))


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.log = io.StringIO()
        token = "t" * 43
        oidc = ConsoleServer(port=0, token=token, store=Store(root / ".console"), base="/serverless", log=cls.log)
        kcp = KcpServer(token, store=KcpStore(root / ".kcp-console"), log=cls.log, env={})
        cls.portal = Portal(oidc, kcp, port=0, log=cls.log)
        cls.portal.start()
        cls.port, cls.token = cls.portal.port, token

    @classmethod
    def tearDownClass(cls):
        cls.portal.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, token=True, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Console-Token"] = self.token if token is True else token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), raw

    # -- separation ---------------------------------------------------------

    def test_home_is_the_dashboard(self):
        st, hd, raw = self.req("GET", "/", token=False, headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(st, 200)
        self.assertIn(b'src="/dashboard.js"', raw)
        self.assertIn(b'href="/serverless/gcp"', raw)
        self.assertIn(b'href="/k8s/clusters/aks"', raw)
        self.assertIn(self.token.encode(), raw)
        self.assertIn("frame-ancestors 'none'", hd["Content-Security-Policy"])
        # Opened from another site: no token in the page.
        cross = self.req("GET", "/", token=False, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[2]
        self.assertNotIn(self.token.encode(), cross)
        for p in ("/dashboard.js", "/dashboard.css"):
            self.assertEqual(self.req("GET", p, token=False)[0], 200, p)

    def test_each_page_comes_from_its_own_console(self):
        for p in ("/serverless/", "/serverless/gcp", "/serverless/azure/runs"):
            st, _, raw = self.req("GET", p, token=False, headers={"Sec-Fetch-Site": "none"})
            self.assertEqual(st, 200, p)
            self.assertIn(b"<title>JFrog OIDC Console</title>", raw, p)
            self.assertIn(b'<meta name="console-base" content="/serverless">', raw, p)
            self.assertIn(b'href="/serverless/static/styles.css"', raw, p)
            self.assertNotIn(b'"/static/', raw, p)
        for p in ("/k8s/clusters", "/k8s/clusters/aks"):
            st, _, raw = self.req("GET", p, token=False, headers={"Sec-Fetch-Site": "none"})
            self.assertEqual(st, 200, p)
            self.assertIn(b"<title>KCP Console</title>", raw, p)
            self.assertIn(b'<meta name="console-base" content="/k8s">', raw, p)
            self.assertNotIn(b'"/static/', raw, p)

    def test_a_section_does_not_serve_the_others_pages(self):
        self.assertEqual(self.req("GET", "/serverless/clusters/aks", token=False)[0], 404)
        self.assertEqual(self.req("GET", "/k8s/gcp", token=False)[0], 404)

    def test_apis_are_separate(self):
        self.assertEqual(self.req("GET", "/serverless/api/schema")[0], 200)
        self.assertEqual(self.req("GET", "/k8s/api/schema")[0], 404)
        self.assertEqual(self.req("GET", "/k8s/api/knowledge")[0], 200)
        self.assertEqual(self.req("GET", "/serverless/api/knowledge")[0], 404)

    def test_nothing_shared_at_the_root(self):
        for p in ("/api/configs", "/api/session", "/static/app.js"):
            self.assertEqual(self.req("GET", p)[0], 404, p)

    def test_stores_are_separate(self):
        a = json.loads(self.req("GET", "/serverless/api/session")[2])
        self.assertTrue(a["store"].endswith(".console"))
        self.assertEqual(self.req("GET", "/k8s/api/session")[0], 200)
        self.assertNotEqual(self.portal.oidc.store.root, self.portal.kcp.store.root)

    def test_old_paths_redirect_into_their_section(self):
        for old, new in (("/gcp/runs", "/serverless/gcp/runs"), ("/azure?x=1", "/serverless/azure?x=1")):
            st, hd, _ = self.req("GET", old, token=False)
            self.assertEqual((st, hd.get("Location")), (308, new), old)

    # -- security, per section ----------------------------------------------

    def test_one_token_for_both_and_none_is_401(self):
        for p in ("/serverless/api/schema", "/k8s/api/knowledge"):
            self.assertEqual(self.req("GET", p, token=False)[0], 401, p)
            self.assertEqual(self.req("GET", p, token="x" * 43)[0], 401, p)

    def test_bad_host_and_cross_origin_refused_everywhere(self):
        for p in ("/", "/serverless/api/schema", "/k8s/api/knowledge", "/gcp"):
            self.assertEqual(self.req("GET", p, host=f"rebind.example:{self.port}")[0], 403, p)
        for p in ("/serverless/api/validate", "/k8s/api/values"):
            st = self.req("POST", p, {"config": {}}, headers={"Origin": "https://evil.example"})[0]
            self.assertEqual(st, 403, p)

    def test_keep_alive_connection_routes_every_request(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}", "X-Console-Token": self.token}
        for p, want in (("/serverless/api/schema", 200), ("/k8s/api/knowledge", 200), ("/", 200),
                        ("/k8s/api/schema", 404)):
            conn.request("GET", p, headers=h)
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, want, p)
        conn.close()

    def test_portal_log_is_redacted(self):
        self.req("GET", f"/nope?id_token={JWT}", token=False)
        self.assertNotIn(JWT, self.log.getvalue())


if __name__ == "__main__":
    unittest.main()
