"""M2: the server's security model, the API and the redactor."""

import http.client
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from kcp_console import __main__ as cli, redact
from kcp_console.server import KcpServer
from kcp_console.store import Store


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = io.StringIO()
        cls.srv = KcpServer(port=0, log=cls.log, store=Store(Path(cls.tmp.name)))
        cls.srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.srv.port}"}
        if token:
            h["X-Console-Token"] = self.srv.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r, raw

    def jget(self, path, **kw):
        r, raw = self.req("GET", path, **kw)
        return r.status, json.loads(raw or b"null")

    # -- security

    def test_api_needs_the_token(self):
        self.assertEqual(self.req("GET", "/api/session", token=False)[0].status, 401)
        self.assertEqual(self.req("GET", "/api/session", headers={"X-Console-Token": "x" * 43}, token=False)[0].status, 401)
        self.assertEqual(self.jget("/api/session")[0], 200)

    def test_bad_host_is_refused(self):
        self.assertEqual(self.req("GET", "/", host="evil.example:80")[0].status, 403)
        self.assertEqual(self.req("GET", "/api/session", host=f"attacker.test:{self.srv.port}")[0].status, 403)

    def test_cross_origin_is_refused(self):
        r, _ = self.req("POST", "/api/sim", body={"scenario": "happy-path"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("GET", "/api/session", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)

    def test_security_headers(self):
        r, _ = self.req("GET", "/", token=False)
        self.assertEqual(r.status, 200)
        csp = r.getheader("Content-Security-Policy")
        for part in ("script-src 'self'", "style-src 'self'", "frame-ancestors 'none'", "form-action 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_static_allow_list(self):
        for p in ("/static/../server.py", "/static/%2e%2e/server.py", "/server.py", "/static/nope.js"):
            self.assertEqual(self.req("GET", p, token=False)[0].status, 404, p)
        self.assertEqual(self.req("GET", "/static/app.js", token=False)[0].status, 200)

    def test_page_routes_serve_the_app(self):
        for p in ("/clusters", "/configure", "/runs"):
            r, raw = self.req("GET", p, token=False)
            self.assertEqual(r.status, 200, p)
            self.assertIn(b'<script type="module" src="/static/app.js">', raw, p)
        for p in ("/operate/nope", "/docs", "/clusters/x"):
            self.assertEqual(self.req("GET", p, token=False)[0].status, 404, p)

    def test_json_only_and_size_cap(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("POST", "/api/sim", body=b"hi", headers={"Host": f"127.0.0.1:{self.srv.port}",
                                                          "X-Console-Token": self.srv.token, "Content-Type": "text/plain"})
        self.assertEqual(c.getresponse().status, 415)
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest("POST", "/api/sim")
        c.putheader("Host", f"127.0.0.1:{self.srv.port}")
        c.putheader("X-Console-Token", self.srv.token)
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(2 * 1024 * 1024))
        c.endheaders()
        c.send(b"{" + b" " * (2 * 1024 * 1024 - 2) + b"}")
        self.assertEqual(c.getresponse().status, 413)
        c.close()

    def test_no_inline_script_or_style_in_static(self):
        html = (Path(__file__).parents[1] / "static" / "index.html").read_text()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn("<style", html)
        self.assertNotRegex(html, r"\sstyle=")
        self.assertNotRegex(html, r"\son[a-z]+=")
        for f in (Path(__file__).parents[1] / "static").iterdir():
            text = f.read_text()
            self.assertNotRegex(text, r"https?://(?!127\.0\.0\.1|www\.w3\.org/2000/svg)", f.name)
            self.assertNotIn("innerHTML", text, f.name)

    def test_bind_refuses_non_loopback(self):
        self.assertIsNone(cli.check_bind("127.0.0.1", False))
        self.assertIsNotNone(cli.check_bind("0.0.0.0", False))
        self.assertIsNone(cli.check_bind("0.0.0.0", True))

    # -- API

    def test_knowledge_and_scenarios(self):
        st, k = self.jget("/api/knowledge")
        self.assertEqual(st, 200)
        self.assertIn("STATUS_AT_A_GLANCE", k)
        st, sc = self.jget("/api/sim/scenarios")
        self.assertEqual(len(sc["scenarios"]), 23)

    def test_sim_run_fetch_and_events(self):
        r, raw = self.req("POST", "/api/sim", body={"scenario": "stale-node-entry-after-fix"})
        self.assertEqual(r.status, 200)
        sim = json.loads(raw)
        self.assertEqual((sim["verdict"], sim["root_cause"], sim["mismatches"]), ("FAIL", "doubled-https", []))
        st, again = self.jget(f"/api/sim/{sim['sim_id']}")
        self.assertEqual(again["root_cause"], "doubled-https")
        r, raw = self.req("GET", f"/api/sim/{sim['sim_id']}/events")
        self.assertEqual(r.getheader("Content-Type").split(";")[0], "text/event-stream")
        self.assertTrue(raw.decode().rstrip().split("\n\n")[-1].startswith("event: done"))

    def test_sim_refuses_unknown_names(self):
        for name in ("../world", "nope", "", "A"):
            self.assertEqual(self.req("POST", "/api/sim", body={"scenario": name})[0].status, 400, name)
        self.assertEqual(self.jget("/api/sim/000000000000")[0], 404)
        self.assertEqual(self.jget("/api/sim/../../etc")[0], 404)

    def test_configs_roundtrip_and_secret_refusal(self):
        r, raw = self.req("PUT", "/api/configs/sandbox-pilot", body={"config": {"cluster": {"name": "aks-sandbox-01"}}})
        self.assertEqual(r.status, 200, raw)
        path = Path(self.tmp.name) / "configs" / "sandbox-pilot.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.jget("/api/configs")[1]["configs"][0]["name"], "sandbox-pilot")
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
        r, _ = self.req("PUT", "/api/configs/leak", body={"config": {"note": jwt}})
        self.assertEqual(r.status, 400)
        self.assertFalse((Path(self.tmp.name) / "configs" / "leak.json").exists())
        self.assertEqual(self.req("PUT", "/api/configs/Bad..Name", body={"config": {}})[0].status, 400)
        self.assertEqual(self.req("DELETE", "/api/configs/sandbox-pilot")[0].status, 200)

    def test_validate(self):
        r, raw = self.req("POST", "/api/validate", body={"config": {"values": {"artifactory_url": "https://example.jfrog.io"}}})
        issues = json.loads(raw)["issues"]
        self.assertTrue(any(i["level"] == "error" and i["field"] == "values.artifactory_url" for i in issues))

    def test_log_lines_are_redacted(self):
        self.req("GET", "/api/session?access_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abcdef")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", self.log.getvalue())


class RedactTest(unittest.TestCase):
    """Every secret the runbook names, in the shapes it appears in."""

    def test_secret_shapes(self):
        jwt = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEifQ.eyJzdWIiOiJzeXN0ZW06c2VydmljZWFjY291bnQ6amZyb2c6amZyb2ctcHJvdmlkZXItc2EifQ.sig"
        pat = "a" * 20 + "b2c3d4e5f6g7" + "h" * 20
        cases = [
            f"Service Account Token: {jwt}",
            "helm upgrade --install x --set binaryDownload.auth.accessToken=cmVmdGtuOjAxOjE3MDAwMDAwMDA6abc",
            "--set binaryDownload.auth.accessToken=plainsecretvalue",
            "Authorization: Bearer abc.def-ghi",
            "curl -H 'X-JFrog-Art-Api: AKCp8abcdef'",
            "JFROG_DOWNLOAD_TOKEN=s3cretvalue1",
            "JfrogDownloadToken: s3cretvalue2",
            "ARTIFACTORY_ADMIN_TOKEN=adminsecret3",
            '{"artifactory-download-token": "kvsecretvalue4"}',
            f"AZURE_DEVOPS_EXT_PAT={pat}",
            f"pat {pat}",
            "curl -u admin:pw-secret5 https://example.jfrog.io",
            "http://127.0.0.1:8766/#t=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        ]
        secrets = [jwt, "cmVmdGtuOjAxOjE3MDAwMDAwMDA6abc", "plainsecretvalue", "abc.def-ghi", "AKCp8abcdef",
                   "s3cretvalue1", "s3cretvalue2", "adminsecret3", "kvsecretvalue4", pat, pat, "pw-secret5",
                   "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"]
        for line, secret in zip(cases, secrets):
            self.assertNotIn(secret, redact.text(line), line)

    def test_env_values_are_masked(self):
        os.environ["JFROG_ACCESS_TOKEN"] = "an-opaque-env-token-value"
        try:
            self.assertNotIn("an-opaque-env-token-value", redact.text("x an-opaque-env-token-value y"))
        finally:
            del os.environ["JFROG_ACCESS_TOKEN"]

    def test_runbook_facts_survive(self):
        for keep in ("RT oidc token url :https://https://example.jfrog.io/access/api/v1/oidc/token",
                     "--set binaryDownload.auth.accessToken=***", "value: example.jfrog.io",
                     "jfrog_oidc_provider_name: aks-sandbox-oidc", "aks-nodepool1-12345678-vmss00000e",
                     "sha256 3b7a0c4f9e1d2a5b6c8e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2"):
            self.assertEqual(redact.text(keep), keep)

    def test_obj_masks_secret_named_env_items_only(self):
        o = {"env": [{"name": "artifactory_url", "value": "example.jfrog.io"},
                     {"name": "JfrogDownloadToken", "value": "zzz"}], "accessToken": "yyy"}
        r = redact.obj(o)
        self.assertEqual(r["env"][0]["value"], "example.jfrog.io")
        self.assertEqual(r["env"][1]["value"], redact.MASK)
        self.assertEqual(r["accessToken"], redact.MASK)


if __name__ == "__main__":
    unittest.main()


class PageTokenTest(ServerTest):
    """The page carries the token only when opened directly, never for a cross-site load."""

    def test_page_token(self):
        meta = f'name="console-token" content="{self.srv.token}"'
        for site, has in (("none", True), ("same-origin", True), ("cross-site", False), (None, False)):
            h = {"Sec-Fetch-Site": site} if site else {}
            r, raw = self.req("GET", "/", headers=h, token=False)
            self.assertEqual(meta in raw.decode(), has, site)


class DiscoverRaceTest(unittest.TestCase):
    """Two reads of one cluster at once (the page's refresh and a status read): both get the
    result of one discover, neither gets 'still running'."""

    def test_concurrent_reads_share_one_discover(self):
        import threading
        from unittest import mock
        from kcp_console.checks import preflight as pf
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srv = KcpServer(port=0, log=io.StringIO(), store=Store(Path(tmp.name)))
        from kcp_console import model
        srv.store.put("a", model.default_config() | {"name": "a"})
        gate, calls = threading.Event(), []

        def slow(cfg, sh, env=None, kubeconfig=None):
            calls.append(1)
            gate.wait(5)
            return {"cluster": {"ok": True}}

        out, errs = [], []
        with mock.patch.object(pf, "discover", slow), mock.patch.object(pf, "preflight", lambda c, f: {"checks": []}):
            def read():
                try:
                    out.append(srv.discover("a", True))
                except Exception as exc:  # noqa: BLE001 - the test reports it
                    errs.append(exc)
            ts = [threading.Thread(target=read) for _ in range(3)]
            for t in ts:
                t.start()
            import time
            time.sleep(1.5)  # longer than the old 1 s lock timeout
            gate.set()
            for t in ts:
                t.join(10)
        self.assertEqual(errs, [])
        self.assertEqual(len(out), 3)
        self.assertEqual(len(calls), 1)
