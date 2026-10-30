"""M2 acceptance: auth, Host/Origin checks, CSP, the sim API and its SSE stream."""

import http.client
import json
import unittest

from console import server as srv
from console.__main__ import check_bind

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = srv.ConsoleServer(port=0)
        cls.console.start()
        cls.port = cls.console.port
        cls.token = cls.console.token

    @classmethod
    def tearDownClass(cls):
        cls.console.stop()

    def req(self, method, path, body=None, token=True, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Console-Token"] = self.token if token is True else token
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), raw

    # -- security ---------------------------------------------------------------

    def test_no_token_is_401(self):
        self.assertEqual(self.req("GET", "/api/schema", token=False)[0], 401)

    def test_wrong_token_is_401(self):
        self.assertEqual(self.req("GET", "/api/schema", token="x" * 43)[0], 401)

    def test_wrong_host_is_403(self):
        self.assertEqual(self.req("GET", "/api/schema", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("GET", "/", host=f"rebind.attacker.example:{self.port}")[0], 403)

    def test_localhost_host_allowed(self):
        self.assertEqual(self.req("GET", "/api/schema", host=f"localhost:{self.port}")[0], 200)

    def test_cross_origin_is_403(self):
        st = self.req("POST", "/api/validate", {"config": {}}, headers={"Origin": "https://evil.example"})[0]
        self.assertEqual(st, 403)
        ok = self.req("POST", "/api/validate", {"config": {}}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0]
        self.assertEqual(ok, 200)

    def test_cross_site_page_navigation_allowed_but_not_api(self):
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        self.assertEqual(self.req("GET", "/?cloud=gcp", token=False, headers=nav)[0], 200)
        for p in ("/gcp", "/azure/runs", "/gcp/configure", "/azure/simulate", "/gcp/configs"):
            self.assertEqual(self.req("GET", p, token=False, headers=nav)[0], 200, p)
        self.assertEqual(self.req("GET", "/aws/runs", token=False, headers=nav)[0], 404)
        self.assertEqual(self.req("GET", "/api/schema", headers=nav)[0], 403)
        fetch = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}
        self.assertEqual(self.req("GET", "/", token=False, headers=fetch)[0], 403)

    def test_security_headers(self):
        for path, token in (("/", False), ("/api/schema", True)):
            st, headers, _ = self.req("GET", path, token=token)
            self.assertEqual(st, 200, path)
            csp = headers["Content-Security-Policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_static_without_token_and_no_traversal(self):
        st, headers, body = self.req("GET", "/", token=False)
        self.assertIn(b"<script type=\"module\" src=\"/static/app.js\"", body)
        self.assertEqual(self.req("GET", "/static/app.js", token=False)[0], 200)
        for bad in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py", "/static/nope.js"):
            self.assertEqual(self.req("GET", bad, token=False)[0], 404, bad)

    def test_post_needs_json(self):
        st = self.req("POST", "/api/validate", b"{}", headers={"Content-Type": "text/plain"})[0]
        self.assertEqual(st, 415)

    def test_body_limit(self):
        self.assertEqual(self.req("POST", "/api/validate", b"{" + b" " * (2 * 1024 * 1024) + b"}")[0], 413)

    def test_unknown_api_404(self):
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)

    # -- API ----------------------------------------------------------------------

    def test_schema(self):
        body = json.loads(self.req("GET", "/api/schema")[2])
        self.assertIn("azure_container_app", body["kinds"])

    def test_validate(self):
        from console import model
        cfg = model.default_config("azure_container_app", name="orders")
        cfg["pull_path"]["ttl_seconds"] = 3600
        body = json.loads(self.req("POST", "/api/validate", {"config": cfg})[2])
        self.assertEqual(body["errors"], [])
        self.assertIn("pull_ttl_vs_rotation", {w["code"] for w in body["warnings"]})
        self.assertEqual(body["ttl_math"]["pickup_lag_seconds"], 1800)

    def test_scenarios(self):
        body = json.loads(self.req("GET", "/api/sim/scenarios")[2])
        names = {s["name"] for s in body["scenarios"]}
        self.assertIn("rotator-misses-3-runs", names)
        self.assertIn("rotator_down", body["fault_types"])

    def test_sim_bad_request(self):
        self.assertEqual(self.req("POST", "/api/sim", {"scenario": "no-such"})[0], 400)
        self.assertEqual(self.req("POST", "/api/sim", {})[0], 400)
        self.assertEqual(self.req("GET", "/api/sim/doesnotexist")[0], 404)

    def test_sim_sse_stream_and_result(self):
        st, _, raw = self.req("POST", "/api/sim", {"scenario": "rotator-misses-3-runs"})
        self.assertEqual(st, 202)
        sim_id = json.loads(raw)["sim_id"]

        st, headers, raw = self.req("GET", f"/api/sim/{sim_id}/events")
        self.assertEqual(st, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        text = raw.decode()
        frames = [f for f in text.split("\n\n") if f.strip()]
        kinds = [next(l[7:] for l in f.splitlines() if l.startswith("event: ")) for f in frames if "event: " in f]
        self.assertIn("event", kinds)
        self.assertEqual(kinds[-1], "done")
        done = json.loads(next(l[6:] for l in frames[-1].splitlines() if l.startswith("data: ")))
        self.assertEqual(done["verdict"], "fail")

        body = json.loads(self.req("GET", f"/api/sim/{sim_id}")[2])
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["result"]["verdict"], "fail")
        self.assertTrue(body["result"]["chain"])

    def test_sim_overrides_and_faults(self):
        st, _, raw = self.req("POST", "/api/sim", {
            "scenario": "happy-aca", "overrides": {"pull_path.ttl_seconds": 1800},
            "faults": [], "load": {"cold_starts_per_hour": 3}, "duration": "6h"})
        self.assertEqual(st, 202)
        sim_id = json.loads(raw)["sim_id"]
        self.req("GET", f"/api/sim/{sim_id}/events")  # wait for completion
        result = json.loads(self.req("GET", f"/api/sim/{sim_id}")[2])["result"]
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["duration_s"], 6 * 3600)

    def test_sim_rejects_unsafe_overrides(self):
        st = self.req("POST", "/api/sim", {"scenario": "happy-aca", "overrides": {"jfrog.url": "https://x"}})[0]
        self.assertEqual(st, 400)

    def test_sse_lines_are_redacted(self):
        line = srv.sse_frame("event", {"message": f"leak {JWT}", "Authorization": "Bearer abcdef123456"})
        self.assertNotIn(JWT, line)
        self.assertNotIn("abcdef123456", line)


class AccessLogTests(unittest.TestCase):
    def test_access_log_is_redacted(self):
        import io
        log = io.StringIO()
        console = srv.ConsoleServer(port=0, log=log)
        console.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", console.port, timeout=10)
            conn.request("GET", f"/api/sim/{JWT}?access_token=abcdef123456",
                         headers={"Host": f"127.0.0.1:{console.port}", "X-Console-Token": console.token})
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()
        finally:
            console.stop()
        text = log.getvalue()
        self.assertIn("/api/sim/", text)
        self.assertNotIn(JWT, text)
        self.assertNotIn("abcdef123456", text)
        self.assertNotIn(console.token, text)


class BindTests(unittest.TestCase):
    def test_loopback_only_by_default(self):
        self.assertIsNone(check_bind("127.0.0.1", False))
        self.assertIsNotNone(check_bind("0.0.0.0", False))
        self.assertIsNotNone(check_bind("192.168.1.10", False))
        self.assertIsNone(check_bind("0.0.0.0", True))


if __name__ == "__main__":
    unittest.main()
