"""M3 API: saved configs, preflight and discover over HTTP, in fixture-replay mode."""

import http.client
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from console import model
from console import server as srv
from console.store import Store, StoreError

FX = Path(__file__).parent / "fixtures"
SUB = "11111111-2222-3333-4444-555555555555"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


def flex_cfg():
    cfg = model.default_config("azure_functions_premium", name="fn-flex")
    cfg["target"]["azure"].update(subscription=SUB, resource_group="rg-apps", name="fn-flex")
    return cfg


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / ".console")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_permissions(self):
        cfg = model.default_config("gcp_run_service", name="orders")
        self.store.put("orders", cfg)
        self.assertEqual(self.store.get("orders")["target"]["kind"], "gcp_run_service")
        path = self.store.configs / "orders.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([c["name"] for c in self.store.list()], ["orders"])
        self.assertTrue(self.store.delete("orders"))
        self.assertFalse(self.store.delete("orders"))

    def test_name_forces_path_and_rejects_traversal(self):
        cfg = self.store.put("orders", dict(model.default_config("gcp_run_service"), name="other"))
        self.assertEqual(cfg["name"], "orders")
        for bad in ("../x", "a/b", "Upper", "", ".hidden"):
            with self.assertRaises(StoreError):
                self.store.put(bad, {})

    def test_refuses_secrets(self):
        cfg = model.default_config("gcp_run_service", name="orders")
        cfg["verify"]["artifact_path"] = f"repo/{JWT}"
        with self.assertRaises(StoreError):
            self.store.put("orders", cfg)
        cfg = model.default_config("gcp_run_service", name="orders")
        cfg["jfrog"]["password"] = "hunter2"
        with self.assertRaises(StoreError):
            self.store.put("orders", cfg)
        self.assertEqual(self.store.list(), [])


class SessionTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / ".console")

    def tearDown(self):
        self.tmp.cleanup()

    def test_kept_across_restarts_with_owner_only_mode(self):
        first = self.store.session_token()
        self.assertRegex(first, r"^[A-Za-z0-9_-]{40,}$")
        self.assertEqual(self.store.session_token(), first)
        mode = stat.S_IMODE(os.stat(self.store.root / "session-token").st_mode)
        self.assertEqual(mode, 0o600)

    def test_renew_and_malformed_file_replaced(self):
        first = self.store.session_token()
        self.assertNotEqual(self.store.session_token(renew=True), first)
        (self.store.root / "session-token").write_text("not a token!\n")
        self.assertRegex(self.store.session_token(), r"^[A-Za-z0-9_-]{40,}$")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.console = srv.ConsoleServer(port=0, store=Store(Path(cls.tmp.name)), fixtures=FX / "discover",
                                        log=open(os.devnull, "w"))
        cls.console.start()

    @classmethod
    def tearDownClass(cls):
        cls.console.stop()
        cls.console.log.close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.console.port, timeout=30)
        headers = {"Host": f"127.0.0.1:{self.console.port}", "X-Console-Token": self.console.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw or b"null")

    def test_session(self):
        st, body = self.req("GET", "/api/session")
        self.assertEqual(st, 200)
        self.assertEqual(body["mode"], "replay")

    def test_configs_crud(self):
        cfg = model.default_config("gcp_run_service", name="orders")
        st, body = self.req("PUT", "/api/configs/orders", {"config": cfg})
        self.assertEqual(st, 200)
        self.assertIn("validation", body)
        st, body = self.req("GET", "/api/configs")
        self.assertIn("orders", [c["name"] for c in body["configs"]])
        self.assertEqual(self.req("GET", "/api/configs/orders")[1]["config"]["name"], "orders")
        self.assertEqual(self.req("PUT", "/api/configs/..%2fx", {"config": cfg})[0], 400)
        self.assertEqual(self.req("GET", "/api/configs/nope")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/configs/orders")[0], 200)
        self.assertEqual(self.req("DELETE", "/api/configs/orders")[0], 404)

    def test_config_with_secret_refused(self):
        cfg = model.default_config("gcp_run_service", name="orders")
        cfg["jfrog"]["reader_group"] = f"Bearer {JWT}"
        st, body = self.req("PUT", "/api/configs/orders", {"config": cfg})
        self.assertEqual(st, 400)
        self.assertNotIn(JWT, json.dumps(body))

    def test_discover_corrects_kind(self):
        st, body = self.req("POST", "/api/discover", {"config": flex_cfg()})
        self.assertEqual(st, 200)
        self.assertEqual(body["kind"], "azure_functions_flex")
        self.assertEqual(body["corrections"][0]["to"], "azure_functions_flex")
        self.assertNotIn("fixture_misses", body)

    def test_discover_bad_request(self):
        self.assertEqual(self.req("POST", "/api/discover", {"config": {"target": {"kind": "nope"}}})[0], 400)
        self.assertEqual(self.req("POST", "/api/discover", {})[0], 400)

    def test_preflight_in_replay_reports_misses_not_crash(self):
        st, body = self.req("POST", "/api/preflight", {"config": flex_cfg()})
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "fail")
        self.assertTrue(body["fixture_misses"])

    def stream(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.console.port, timeout=30)
        conn.request("POST", path, body=json.dumps(body).encode(),
                     headers={"Host": f"127.0.0.1:{self.console.port}", "X-Console-Token": self.console.token,
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        frames = []
        for block in raw.split("\n\n"):
            kind = next((ln[7:] for ln in block.split("\n") if ln.startswith("event: ")), None)
            data = [ln[6:] for ln in block.split("\n") if ln.startswith("data: ")]
            if kind:
                frames.append((kind, json.loads("\n".join(data))))
        return resp.status, resp.getheader("Content-Type"), frames

    def test_preflight_streams_each_check_and_command_as_it_happens(self):
        st, ctype, frames = self.stream("/api/preflight?stream=1", {"config": flex_cfg()})
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("text/event-stream"))
        kinds = [k for k, _ in frames]
        self.assertEqual(kinds[-1], "done")
        self.assertIn("log", kinds)
        done = frames[-1][1]
        checks = [d for k, d in frames if k == "check"]
        self.assertEqual(len(checks), len(done["checks"]))
        self.assertEqual([c["id"] for c in checks], [c["id"] for c in done["checks"]])
        # The first check arrives before the last command line: it is streamed, not batched at the end.
        self.assertLess(kinds.index("check"), len(kinds) - 1 - kinds[::-1].index("log"))
        self.assertEqual(done["status"], "fail")
        self.assertTrue(done["fixture_misses"])
        # The lock is released once the stream ends: another preflight can start.
        self.assertEqual(self.req("POST", "/api/preflight", {"config": flex_cfg()})[0], 200)


if __name__ == "__main__":
    unittest.main()
