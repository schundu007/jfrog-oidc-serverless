"""Archive: workloads whose resources are gone leave the live pages; kept 7 days, then deleted."""

import http.client
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from console import model
from console import server as srv
from console.archive import DAY, Archive
from console.store import Store

FX = Path(__file__).parent / "fixtures" / "demo"


def cfg(name="orders"):
    c = model.default_config("gcp_run_service", name=name)
    c["target"]["gcp"].update(project="demo-nonprod-1234", region="us-central1", name=name)
    return c


def fake_run(root: Path, rid: str, name: str, status="succeeded"):
    d = root / "runs" / rid
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(cfg(name)))
    (d / "run.json").write_text(json.dumps({"name": name, "kind": "gcp_run_service", "status": status,
                                            "created": "2026-11-01T00:00:00Z", "steps": []}))
    (d / "events.jsonl").write_text(json.dumps({"t": "2026-11-01T00:00:00Z", "type": "log", "text": "hello"}) + "\n")
    return d


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = [1_800_000_000.0]
        self.a = Archive(self.root, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_moves_files_and_runs_and_hides_the_name(self):
        (self.root / "c.json").write_text("{}")
        run = fake_run(self.root, "20261101-000000-aaaaaa", "orders")
        e = self.a.add("orders", "gcp_run_service", "gone", files={"config.json": self.root / "c.json"},
                       runs={"20261101-000000-aaaaaa": run})
        self.assertFalse(run.exists())
        self.assertFalse((self.root / "c.json").exists())
        self.assertEqual(self.a.names(), {"orders"})
        self.assertEqual(e["runs"], ["20261101-000000-aaaaaa"])
        self.assertTrue(self.a.run_dir("20261101-000000-aaaaaa").is_dir())
        self.assertEqual(self.a.config("orders"), {})
        self.assertEqual(self.a.list()[0]["expires_at"], self.now[0] + 7 * DAY)

    def test_purge_after_seven_days_only(self):
        self.a.add("orders", "k", "gone", data={"config.json": {"a": 1}})
        self.now[0] += 7 * DAY - 1
        self.assertEqual(self.a.purge(), [])
        self.now[0] += 1
        self.assertEqual(len(self.a.purge()), 1)
        self.assertEqual(self.a.list(), [])
        self.assertEqual(self.a.names(), set())

    def test_release_and_restore(self):
        self.a.add("orders", "k", "gone", data={"config.json": {"a": 1}})
        self.assertEqual(self.a.release("orders"), 1)
        self.assertEqual(self.a.names(), set())
        self.assertEqual(len(self.a.list()), 1)   # still kept, still readable
        run = fake_run(self.root, "20261101-000000-bbbbbb", "orders")
        self.a.add("orders", "k", "gone", files={}, runs={"20261101-000000-bbbbbb": run})
        back = self.a.restore("orders", runs_dir=self.root / "runs")
        self.assertEqual(back["runs"], ["20261101-000000-bbbbbb"])
        self.assertTrue((self.root / "runs" / "20261101-000000-bbbbbb").is_dir())
        self.assertEqual(self.a.names(), set())

    def test_drop_run_deletes_one_archived_run(self):
        run = fake_run(self.root, "20261101-000000-dddddd", "orders")
        e = self.a.add("orders", "k", "gone", runs={"20261101-000000-dddddd": run})
        self.assertTrue(self.a.drop_run("20261101-000000-dddddd"))
        self.assertIsNone(self.a.run_dir("20261101-000000-dddddd"))
        self.assertEqual(self.a.list()[0]["runs"], [])
        self.assertFalse(self.a.drop_run("20261101-000000-dddddd"))

    def test_run_dir_rejects_traversal(self):
        self.assertIsNone(self.a.run_dir("../x"))
        self.assertIsNone(self.a.run_dir(".."))


class ServerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.console = srv.ConsoleServer(port=0, store=Store(self.root), fixtures=FX, log=open(os.devnull, "w"))
        self.console.store.put("orders", cfg("orders"))
        self.console.store.put("billing", cfg("billing"))
        self.rid = "20261101-000000-cccccc"
        fake_run(self.root, self.rid, "orders")
        self.console.runs.adopt([self.rid])
        self.state = {"orders": "missing", "billing": "unknown"}
        self.console.live_status = lambda cloud: {n: {"state": s} for n, s in self.state.items()}
        self.console.start()

    def tearDown(self):
        self.console.stop()
        self.console.log.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.console.port, timeout=30)
        headers = {"Host": f"127.0.0.1:{self.console.port}", "X-Console-Token": self.console.token}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw or b"null")

    def test_missing_is_archived_unknown_is_not(self):
        st, body = self.req("GET", "/api/live?cloud=gcp")
        self.assertEqual(st, 200)
        names = [c["name"] for c in self.req("GET", "/api/configs")[1]["configs"]]
        self.assertNotIn("orders", names)
        self.assertIn("billing", names)
        self.assertEqual(self.req("GET", "/api/runs")[1]["runs"], [])
        # Still readable, marked archived.
        st, snap = self.req("GET", f"/api/runs/{self.rid}")
        self.assertEqual((st, snap["archived"]), (200, True))
        st, c = self.req("GET", "/api/configs/orders")
        self.assertEqual((st, c["source"]), (200, "archive"))
        st, arch = self.req("GET", "/api/archive")
        self.assertEqual(arch["retention_days"], 7)
        self.assertEqual(arch["entries"][0]["name"], "orders")
        self.assertEqual(arch["entries"][0]["run_rows"][0]["id"], self.rid)
        # An archived run cannot be approved or cancelled.
        self.assertEqual(self.req("POST", f"/api/runs/{self.rid}/cancel", {})[0], 404)

    def test_back_when_it_exists_again(self):
        self.console.sweep("gcp")
        self.state["orders"] = "wired"
        self.console.sweep("gcp")
        names = [c["name"] for c in self.req("GET", "/api/configs")[1]["configs"]]
        self.assertIn("orders", names)
        self.assertEqual([r["id"] for r in self.req("GET", "/api/runs")[1]["runs"]], [self.rid])
        self.assertEqual(self.console.archive.list(), [])

    def test_saving_again_releases_the_name(self):
        self.console.sweep("gcp")
        st, _ = self.req("PUT", "/api/configs/orders", {"config": cfg("orders")})
        self.assertEqual(st, 200)
        names = [c["name"] for c in self.req("GET", "/api/configs")[1]["configs"]]
        self.assertIn("orders", names)
        self.assertEqual(self.req("GET", f"/api/runs/{self.rid}")[0], 200)   # old run still in the archive

    def test_purged_after_seven_days(self):
        self.console.sweep("gcp")
        self.console.archive.clock = lambda: time.time() + 7 * DAY + 1
        self.console.sweep("gcp")
        self.assertEqual(self.req("GET", f"/api/runs/{self.rid}")[0], 404)
        self.assertEqual(self.console.archive.list(), [])

    def test_onboarded_from_git_stays_hidden_after_the_archive_is_emptied(self):
        """A workload known only from git (an onboarding workspace) that is gone stays off the live pages
        when its archive entry is deleted, and comes back when it exists again."""
        self.console.delivered = lambda: {"shipped": cfg("shipped")}
        self.state["shipped"] = "missing"
        self.console.sweep("gcp")
        for e in self.console.archive.list():          # the operator empties the archive
            shutil.rmtree(self.console.archive.dir / e["id"])
        names = [c["name"] for c in self.req("GET", "/api/configs")[1]["configs"]]
        self.assertNotIn("shipped", names)
        self.console.sweep("gcp")                        # still gone: not archived again
        self.assertEqual(self.console.archive.list(), [])
        self.assertNotIn("shipped", [c["name"] for c in self.req("GET", "/api/configs")[1]["configs"]])
        self.state["shipped"] = "wired"
        self.console.sweep("gcp")
        names = [c["name"] for c in self.req("GET", "/api/configs")[1]["configs"]]
        self.assertIn("shipped", names)


if __name__ == "__main__":
    unittest.main()
