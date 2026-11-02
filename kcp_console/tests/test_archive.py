"""Archive: clusters that no longer exist leave the live pages; kept 7 days, then deleted."""

import http.client
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from kcp_console import model
from kcp_console.archive import DAY
from kcp_console.server import KcpServer
from kcp_console.store import Store
from kcp_console.tests.mount import TOKEN, Mounted, path as k8s

RID = "20261101-000000-dddddd"


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.srv = KcpServer(TOKEN, log=io.StringIO(), store=Store(root))
        for n in ("gone", "here"):
            self.srv.store.put(n, model.default_config() | {"name": n})
        self.srv.status.update("gone", {"notes": "x"})
        run = {"id": RID, "config": "gone", "kind": "pipeline", "name": "prove", "status": "succeeded",
               "created": "2026-11-01T00:00:00Z", "events": [{"t": "2026-11-01T00:00:00Z", "kind": "log", "text": "hi"}]}
        self.srv.run_store.save(run)
        self.srv.run_store.save_log(RID, "the log")
        self.srv.runs.adopt([RID])
        self.state = {"gone": "missing", "here": "unknown"}
        self.srv.fleet_live = lambda: dict(self.state)
        self.mount = Mounted(self.srv)

    def tearDown(self):
        self.mount.stop()
        self.tmp.cleanup()

    def get(self, p, method="GET", body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.srv.port}", "X-Console-Token": TOKEN}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, k8s(p), body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw and r.getheader("Content-Type", "").startswith("application/json") else raw)

    def test_missing_cluster_is_archived(self):
        self.assertEqual(self.get("/api/fleet/live")[0], 200)
        names = [c["name"] for c in self.get("/api/configs")[1]["configs"]]
        self.assertEqual(names, ["here"])
        self.assertEqual([c["name"] for c in self.srv.fleet()["clusters"]], ["here"])
        self.assertEqual(self.get("/api/runs")[1]["runs"], [])
        st, run = self.get(f"/api/runs/{RID}")
        self.assertEqual((st, run["archived"]), (200, True))
        self.assertEqual(self.get(f"/api/runs/{RID}/log")[1]["log"], "the log")
        st, raw = self.get(f"/api/runs/{RID}/events")
        self.assertIn(b"event: done", raw)
        self.assertEqual(self.get("/api/configs/gone")[0], 200)
        arch = self.get("/api/archive")[1]
        self.assertEqual(arch["entries"][0]["name"], "gone")
        self.assertEqual(arch["entries"][0]["run_rows"][0]["id"], RID)
        self.assertEqual(self.get(f"/api/runs/{RID}/cancel", "POST", {})[0], 404)

    def test_back_when_it_exists_again(self):
        self.srv.sweep()
        self.state["gone"] = "present"
        self.srv.sweep()
        self.assertEqual(sorted(c["name"] for c in self.srv.saved()), ["gone", "here"])
        self.assertEqual(self.srv.status.get("gone"), {"notes": "x"})
        self.assertEqual([r["id"] for r in self.srv.runs.list()], [RID])

    def test_purged_after_seven_days(self):
        self.srv.sweep()
        self.srv.archive.clock = lambda: time.time() + 7 * DAY + 1
        self.srv.sweep()
        self.assertEqual(self.get(f"/api/runs/{RID}")[0], 404)
        self.assertEqual(self.srv.archive.list(), [])


if __name__ == "__main__":
    unittest.main()
