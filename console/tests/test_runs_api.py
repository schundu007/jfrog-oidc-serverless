"""M4 API: a whole pipeline run over HTTP in replay mode (recorded cloud, fake terraform)."""

import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from console import model
from console import server as srv
from console.store import Store

FX = Path(__file__).parent / "fixtures" / "demo"


def demo_cfg():
    cfg = model.default_config("gcp_run_service", name="orders")
    cfg["target"]["gcp"].update(project="demo-nonprod-1234", region="us-central1", name="orders")
    cfg["pull_path"]["rotator_image"] = "us-central1-docker.pkg.dev/demo-nonprod-1234/platform/jfrog-oidc-rotator:0.1.0"
    return cfg


class RunsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.console = srv.ConsoleServer(port=0, store=Store(Path(self.tmp.name)), fixtures=FX, log=open(os.devnull, "w"))
        self.console.start()

    def tearDown(self):
        self.console.stop()
        self.console.log.close()
        self.tmp.cleanup()

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

    def wait(self, run_id, *statuses, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st, snap = self.req("GET", f"/api/runs/{run_id}")
            self.assertEqual(st, 200)
            if snap["status"] in statuses:
                return snap
            time.sleep(0.05)
        self.fail(json.dumps(snap["steps"], indent=1)[:4000])

    @staticmethod
    def step(snap, name):
        return next(s for s in snap["steps"] if s["name"] == name)

    def test_full_pipeline_with_gates(self):
        st, body = self.req("POST", "/api/runs", {"config": demo_cfg()})
        self.assertEqual(st, 202, body)
        rid = body["run_id"]
        snap = self.wait(rid, "waiting_approval", "failed")
        self.assertEqual(snap["status"], "waiting_approval", json.dumps(snap["steps"], indent=1)[:3000])
        for name in ("preflight", "discover", "simulate", "plan"):
            self.assertEqual(self.step(snap, name)["status"], "succeeded", name)
        self.assertEqual(self.step(snap, "simulate")["evidence"]["verdict"]["status"], "pass")
        approval = self.step(snap, "apply")["approval"]

        st, body = self.req("POST", f"/api/runs/{rid}/approve", {"step": "apply", "plan_sha256": "f" * 64})
        self.assertEqual((st, body["code"]), (409, "plan_hash_mismatch"))
        st, body = self.req("POST", f"/api/runs/{rid}/approve", {"step": "apply", "plan_sha256": approval["plan_sha256"]})
        self.assertEqual(st, 200, body)

        snap = self.wait(rid, "waiting_approval", "failed")
        wire = self.step(snap, "wire")
        self.assertEqual(wire["status"], "needs_approval", json.dumps(snap["steps"], indent=1)[:3000])
        keys = {d["setting"] for d in wire["evidence"]["diff"]["diff"]}
        self.assertEqual(keys, {"JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SOURCE"})
        st, body = self.req("POST", f"/api/runs/{rid}/approve", {"step": "wire", "diff_sha256": wire["approval"]["diff_sha256"]})
        self.assertEqual(st, 200, body)
        snap = self.wait(rid, "succeeded", "failed")
        self.assertEqual(snap["status"], "succeeded", json.dumps(snap["steps"], indent=1)[:3000])
        self.assertTrue(self.step(snap, "wire")["evidence"]["result"]["verified"])
        # Steps that aren't built yet are flagged so the UI can leave them out.
        self.assertTrue(self.step(snap, "verify")["planned"])
        self.assertFalse(self.step(snap, "wire")["planned"])

        st, body = self.req("GET", "/api/runs")
        self.assertEqual(body["runs"][0]["id"], rid)
        # discovered facts were not saved: this config was never saved to the store
        self.assertEqual(self.console.store.list(), [])

    def test_deleting_a_setup_deletes_its_finished_runs(self):
        cfg = demo_cfg()
        self.assertEqual(self.req("PUT", "/api/configs/orders", {"config": cfg})[0], 200)
        st, body = self.req("POST", "/api/runs", {"config": cfg})
        rid = body["run_id"]
        self.wait(rid, "waiting_approval", "failed")
        # An active run is kept: it may be part-way through changing the target.
        st, body = self.req("DELETE", "/api/configs/orders")
        self.assertEqual((st, body["runs_deleted"], body["runs_kept"]), (200, 0, 1))
        self.req("POST", f"/api/runs/{rid}/cancel", {})
        self.wait(rid, "cancelled", "failed")
        self.assertEqual(self.req("PUT", "/api/configs/orders", {"config": cfg})[0], 200)
        st, body = self.req("DELETE", "/api/configs/orders")
        self.assertEqual((st, body["runs_deleted"], body["runs_kept"]), (200, 1, 0))
        self.assertEqual(self.req("GET", f"/api/runs/{rid}")[0], 404)
        self.assertFalse((Path(self.tmp.name) / "runs" / rid).exists())

    def test_bad_requests(self):
        self.assertEqual(self.req("POST", "/api/runs", {"config": "x"})[0], 400)
        st, body = self.req("POST", "/api/runs", {"config": demo_cfg(), "steps": ["apply"]})
        self.assertEqual((st, body["code"]), (400, "apply_needs_plan"))
        self.assertEqual(self.req("GET", "/api/runs/../../etc")[0], 404)
        self.assertEqual(self.req("GET", "/api/runs/20260101-000000-abcdef")[0], 404)

    def test_run_events_stream(self):
        st, body = self.req("POST", "/api/runs", {"config": demo_cfg(), "steps": ["plan"]})
        rid = body["run_id"]
        self.wait(rid, "succeeded", "failed")
        conn = http.client.HTTPConnection("127.0.0.1", self.console.port, timeout=30)
        conn.request("GET", f"/api/runs/{rid}/events",
                     headers={"Host": f"127.0.0.1:{self.console.port}", "X-Console-Token": self.console.token})
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertIn("event: snapshot", text)
        self.assertIn("event: done", text)
        self.assertIn('"kind":"step"', text)


if __name__ == "__main__":
    unittest.main()
