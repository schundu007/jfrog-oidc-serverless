"""Settings: sign-ins entered on the page, kept out of files, logs and answers."""

import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from console import deliver
from console.shell import Shell
from portal.connections import Connections, SettingsError

TOKEN = "cmVmdGtuOjAxOjE3MDAwMDAwMDA6c2VjcmV0dmFsdWVzZWNyZXQ"   # a JFrog reference token shape
ADO_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG8ifQ.c2lnbmF0dXJlYWRv"

FAKE_AZ = """#!{py}
import sys
if sys.argv[1:3] == ["login", "--use-device-code"]:
    print("To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABCD2345 to authenticate.", file=sys.stderr, flush=True)
    sys.exit(0)
if sys.argv[1:2] == ["logout"]:
    sys.exit(0)
sys.exit(3)
"""
FAKE_GCLOUD = """#!{py}
import sys
if sys.argv[1:3] == ["auth", "login"]:
    print("Go to the following link in your browser, and complete the sign-in prompts:", file=sys.stderr)
    print("    https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=x&code_challenge=y", file=sys.stderr)
    sys.stderr.write("Once finished, enter the verification code provided in your browser: ")
    sys.stderr.flush()
    code = sys.stdin.readline().strip()
    sys.exit(0 if code == "4/goodcode" else 1)
if sys.argv[1:3] == ["auth", "revoke"]:
    sys.exit(0)
if sys.argv[1:4] == ["config", "get-value", "project"]:
    import os
    d = os.environ.get("CLOUDSDK_CONFIG", "")
    f = os.path.join(d, "project")
    print(open(f).read() if os.path.exists(f) else ("machine-proj" if d.endswith("machine-gcloud") else ""))
    sys.exit(0)
if sys.argv[1:4] == ["config", "set", "project"]:
    import os
    d = os.environ["CLOUDSDK_CONFIG"]
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "project"), "w").write(sys.argv[4])
    sys.exit(0)
sys.exit(3)
"""


def fixtures(token_present=True):
    fx = [
        {"argv": ["gcloud", "config", "list", "--format=json"], "stdout": json.dumps({"core": {"account": "me@example.com", "project": "proj-123456"}})},
        {"argv": ["az", "account", "show", "-o", "json"],
         "stdout": json.dumps({"user": {"name": "me@contoso.com"}, "name": "Test sub", "id": "0" * 8 + "-0000-0000-0000-" + "0" * 12, "tenantId": "t1"})},
        {"argv": ["az", "account", "get-access-token", "--resource", "499b84ac-1321-427f-aa17-267ca6975798", "--query", "accessToken", "-o", "tsv"],
         "stdout": ADO_TOKEN + "\n"},
        {"http": "GET https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1", "status": 200,
         "body": json.dumps({"emailAddress": "me@contoso.com"})},
        {"http": "GET https://dev.azure.com/acme/_apis/projects/web?api-version=7.1", "status": 404, "body": "{}"},
        {"argv": ["aws", "configure", "list-profiles"], "stdout": "default\nsso-dev\n"},
        {"argv": ["aws", "sts", "get-caller-identity", "--output", "json"],
         "stdout": json.dumps({"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/dev/me"})},
        {"http": "GET https://acme.jfrog.io/access/api/v1/oidc", "status": 200, "body": "[]"},
    ]
    return fx


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {"PATH": os.environ.get("PATH", "")}
        self.log = []
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in (("az", FAKE_AZ), ("gcloud", FAKE_GCLOUD)):
            p = bin_dir / name
            p.write_text(body.format(py=sys.executable))
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.bin = bin_dir

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self, replay=True, **kw):
        def factory():
            if replay:
                return Shell(mode="replay", fixtures=fixtures(), log=self.log.append)
            return Shell(mode="live", log=self.log.append, which=lambda n: str(self.bin / n) if (self.bin / n).exists() else None)
        return Connections(self.root, shell_factory=factory, environ=self.env, **kw)

    def test_status_reads_every_tool(self):
        c = self.conn()
        c._update("ado", {"organization": "acme", "project": "web", "repository": "r"})
        c._update("jfrog", {"url": "https://acme.jfrog.io"})
        c._jfrog_token = TOKEN
        c.apply()
        s = c.status()
        self.assertEqual(s["gcp"]["account"], "me@example.com")
        self.assertEqual(s["gcp"]["source"], "machine")
        self.assertTrue(s["azure"]["signed_in"])
        self.assertEqual(s["ado"]["account"], "me@contoso.com")
        self.assertFalse(s["ado"]["project_ok"])
        self.assertIn("Project web in acme", s["ado"]["error"])
        self.assertEqual(s["aws"]["profiles"], ["default", "sso-dev"])
        self.assertTrue(s["jfrog"]["signed_in"] and s["jfrog"]["admin"])
        text = json.dumps(s) + "\n".join(self.log)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(ADO_TOKEN, text)

    def test_jfrog_token_is_memory_only(self):
        c = self.conn()
        out = c.sign_in("jfrog", {"url": "https://acme.jfrog.io/", "token": TOKEN})
        self.assertTrue(out["admin"])
        self.assertEqual(self.env["JFROG_ACCESS_TOKEN"], TOKEN)
        saved = (self.root / "connections" / "settings.json").read_text()
        self.assertNotIn(TOKEN, saved)
        self.assertEqual(json.loads(saved)["jfrog"], {"url": "https://acme.jfrog.io"})
        self.assertEqual(stat.S_IMODE((self.root / "connections").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "connections" / "settings.json").stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, json.dumps(c.status()))
        c.sign_out("jfrog")
        self.assertNotIn("JFROG_ACCESS_TOKEN", self.env)
        # A fresh process has no token: it was never written.
        self.assertFalse(self.conn().status()["jfrog"]["token_present"])

    def test_jfrog_rejected_token_is_not_kept(self):
        c = self.conn()
        c.shell_factory = lambda: Shell(mode="replay", fixtures=[
            {"http": "GET https://acme.jfrog.io/access/api/v1/oidc", "status": 401, "body": ""}])
        with self.assertRaises(SettingsError) as e:
            c.sign_in("jfrog", {"url": "https://acme.jfrog.io", "token": TOKEN})
        self.assertIn("rejected", str(e.exception))
        self.assertNotIn("JFROG_ACCESS_TOKEN", self.env)

    def test_bad_inputs_refused(self):
        c = self.conn()
        for tool, body in (("jfrog", {"url": "http://acme.jfrog.io", "token": TOKEN}),
                           ("azure", {"tenant": "bad tenant;"})):
            with self.assertRaises(SettingsError):
                c.sign_in(tool, body)
        with self.assertRaises(SettingsError):
            c.set_values("ado", {"organization": "acme"})
        with self.assertRaises(SettingsError):
            c.set_values("aws", {"region": "nowhere"})

    def test_saving_a_secret_shaped_value_is_refused(self):
        c = self.conn()
        with self.assertRaises(SettingsError):
            c._update("ado", {"organization": ADO_TOKEN})

    def test_azure_device_code_sign_in_switches_to_console_dir(self):
        c = self.conn(replay=False)
        c.sign_in("azure", {"tenant": "contoso.onmicrosoft.com"})
        sess = c._signin["azure"]["session"]
        self.assertEqual(sess.wait(10), 0)
        p = c._pending("azure")
        self.assertEqual(p["code"], "ABCD2345")
        self.assertEqual(p["url"], "https://microsoft.com/devicelogin")
        for _ in range(100):
            if self.env.get("AZURE_CONFIG_DIR"):
                break
            time.sleep(0.05)
        self.assertEqual(self.env["AZURE_CONFIG_DIR"], str(self.root / "connections" / "azure"))
        self.assertEqual(c.settings()["azure"], {"mode": "console", "tenant": "contoso.onmicrosoft.com"})
        c.sign_out("azure")
        self.assertNotIn("AZURE_CONFIG_DIR", self.env)
        self.assertFalse((self.root / "connections" / "azure").exists())
        self.assertEqual(c.settings()["azure"], {"tenant": "contoso.onmicrosoft.com"})

    def test_google_code_goes_to_stdin_and_is_never_logged(self):
        self.env["CLOUDSDK_CONFIG"] = str(self.root / "machine-gcloud")
        c = self.conn(replay=False)
        c.sign_in("gcp", {})
        for _ in range(100):
            p = c._pending("gcp")
            if p.get("url") and any("verification code" in line for line in p["lines"]):
                break
            time.sleep(0.05)
        self.assertTrue(p["url"].startswith("https://accounts.google.com/o/oauth2/auth?"))
        self.assertTrue(p["needs_code"])
        c.send_code("gcp", "4/goodcode")
        self.assertEqual(c._signin["gcp"]["session"].wait(10), 0)
        for _ in range(100):
            if self.env.get("CLOUDSDK_CONFIG", "").endswith("connections/gcloud"):
                break
            time.sleep(0.05)
        self.assertEqual(self.env["CLOUDSDK_CONFIG"], str(self.root / "connections" / "gcloud"))
        # The new login starts on the machine login's project.
        self.assertEqual((self.root / "connections" / "gcloud" / "project").read_text(), "machine-proj")
        self.assertNotIn("4/goodcode", "\n".join(self.log) + json.dumps(c._pending("gcp")))
        with self.assertRaises(SettingsError):
            c.send_code("gcp", "again")

    def test_failed_google_sign_in_keeps_the_machine_login(self):
        self.env["CLOUDSDK_CONFIG"] = "/machine/gcloud"
        c = self.conn(replay=False)
        c.sign_in("gcp", {})
        time.sleep(0.3)
        c.send_code("gcp", "wrong")
        self.assertEqual(c._signin["gcp"]["session"].wait(10), 1)
        time.sleep(0.1)
        self.assertEqual(c._pending("gcp")["state"], "failed")
        self.assertEqual(self.env["CLOUDSDK_CONFIG"], "/machine/gcloud")

    def test_ado_values_retarget_and_override_delivery(self):
        seen = []
        c = self.conn(on_ado=seen.append)
        c.set_values("ado", {"organization": "acme", "project": "web", "repository": "app", "target_branch": ""})
        self.assertEqual(seen, [{"organization": "acme", "project": "web", "repository": "app"}])
        delivery = self.root / "delivery.json"
        delivery.write_text(json.dumps({"ado": {"organization": "old", "project": "p", "repository": "r",
                                                "target_branch": "main"}, "merge": "direct"}))
        stacks = self.root / "stacks"
        stacks.mkdir()
        d = deliver.load_settings(delivery, stacks_dir=stacks, overrides=c.file)
        self.assertEqual(d["ado"], {"organization": "acme", "project": "web", "repository": "app", "target_branch": "main"})
        # Settings alone are enough when the repo has no delivery.json.
        d2 = deliver.load_settings(self.root / "missing.json", stacks_dir=stacks, overrides=c.file)
        self.assertEqual(d2["ado"]["organization"], "acme")
        self.assertIsNone(deliver.load_settings(self.root / "missing.json", stacks_dir=stacks))

    def test_apply_restores_machine_logins(self):
        self.env.update({"AZURE_CONFIG_DIR": "/machine/azure", "AWS_PROFILE": "machine"})
        c = self.conn()
        (self.root / "connections" / "azure").mkdir(parents=True)
        c._update("azure", {"mode": "console"})
        c._update("aws", {"profile": "sso-dev", "region": "eu-west-1"})
        c.apply()
        self.assertEqual(self.env["AZURE_CONFIG_DIR"], str(self.root / "connections" / "azure"))
        self.assertEqual((self.env["AWS_PROFILE"], self.env["AWS_REGION"]), ("sso-dev", "eu-west-1"))
        c._update("azure", None)
        c._update("aws", None)
        c.apply()
        self.assertEqual(self.env["AZURE_CONFIG_DIR"], "/machine/azure")
        self.assertEqual(self.env["AWS_PROFILE"], "machine")
        self.assertNotIn("AWS_REGION", self.env)


class SettingsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from console.server import ConsoleServer
        from console.store import Store
        from kcp_console.server import KcpServer
        from kcp_console.store import Store as KcpStore
        from portal.server import Portal
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        token = "s" * 43
        log = io.StringIO()
        oidc = ConsoleServer(port=0, token=token, store=Store(root / ".console"), base="/serverless", log=log)
        kcp = KcpServer(token, store=KcpStore(root / ".kcp-console"), log=log, env={})
        env = {}
        conn = Connections(root / ".console", environ=env,
                           shell_factory=lambda: Shell(mode="replay", fixtures=fixtures()))
        cls.portal = Portal(oidc, kcp, port=0, log=log, connections=conn)
        cls.portal.start()
        cls.port, cls.token = cls.portal.port, token

    @classmethod
    def tearDownClass(cls):
        cls.portal.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, token=True, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if token:
            h["X-Console-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = r.status, r.read().decode()
        conn.close()
        return out

    def test_needs_token_and_same_origin(self):
        self.assertEqual(self.req("GET", "/api/settings", token=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/settings", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/settings", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_status_and_jfrog_sign_in(self):
        st, body = self.req("GET", "/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["gcp"]["account"], "me@example.com")
        st, body = self.req("POST", "/api/settings/jfrog/signin", {"url": "https://acme.jfrog.io", "token": TOKEN})
        self.assertEqual(st, 200, body)
        st, body = self.req("GET", "/api/settings")
        self.assertNotIn(TOKEN, body)
        self.assertTrue(json.loads(body)["jfrog"]["token_present"])

    def test_errors(self):
        self.assertEqual(self.req("GET", "/api/settings/nope")[0], 404)
        self.assertEqual(self.req("POST", "/api/settings/gcp/code", {"code": "x"})[0], 409)
        self.assertEqual(self.req("PUT", "/api/settings/ado", {"organization": "a"})[0], 400)
        self.assertEqual(self.req("DELETE", "/api/settings/ado")[0], 405)

    def test_page_served(self):
        st, body = self.req("GET", "/settings", token=False)
        self.assertEqual(st, 200)
        self.assertIn("settings-page.js", body)
