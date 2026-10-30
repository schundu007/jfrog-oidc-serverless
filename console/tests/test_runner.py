"""Pipeline runner (spec §4): step state machine, approval gates, cancel, events. Fake terraform; replayed cloud."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from console import model
from console.runner import RunError, RunManager, STEPS
from console.shell import Shell, load_fixtures
from console.steps import tfgen

FAKE = Path(__file__).parent / "fixtures" / "fake_terraform"
FX = Path(__file__).parent / "fixtures"
P, R = "demo-nonprod-1234", "us-central1"
TOKEN = "test-admin-token-value-123"
ENV = {"JFROG_URL": "https://acme.jfrog.io", "JFROG_OIDC_PROVIDER": "gcp-cloudrun", "JFROG_OIDC_AUDIENCE": "jfrog-gcp",
       "JFROG_OIDC_SOURCE": "gcp"}


def cfg():
    c = model.default_config("gcp_run_service", name="orders")
    c["target"]["gcp"].update(project=P, region=R, name="orders")
    c["pull_path"]["rotator_image"] = f"{R}-docker.pkg.dev/{P}/platform/jfrog-oidc-rotator:0.1.0"
    c["target"]["discovered"] = {"kind": "gcp_run_service", "mapping_subject": "104857600000000000001"}
    return c


def describe(env, consume=False):
    fx = {"argv": ["gcloud", "run", "services", "describe", "orders", "--region", R, "--project", P, "--format=json"],
          "stdout": json.dumps({"spec": {"template": {"spec": {"containers": [{"env": [{"name": k, "value": v} for k, v in env.items()]}]}}}})}
    if consume:
        fx["consume"] = True
    return fx


UPDATE = {"argv": ["gcloud", "run", "services", "update", "orders", "--region", R, "--project", P,
                   "--update-env-vars=^@^" + "@".join(f"{k}={v}" for k, v in sorted(ENV.items())), "--quiet"]}


class Base(unittest.TestCase):
    cloud_fixtures = [describe({}, consume=True), describe({}, consume=True), UPDATE, describe(ENV)]

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="runner-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        fixtures = list(self.cloud_fixtures)
        self.mgr = RunManager(
            self.root,
            cloud_shell=lambda log: Shell(mode="replay", fixtures=[dict(f) for f in fixtures], log=log),
            tf_shell=lambda log: Shell(mode="live", log=log, which=lambda n: str(FAKE) if n == "terraform" else None),
            env={"JFROG_ACCESS_TOKEN": TOKEN, "PATH": "/usr/bin:/bin"},
        )
        self.addCleanup(self.mgr.shutdown)

    def ws(self, c=None):
        return tfgen.workspace_dir(self.root, tfgen.workspace_key(c or cfg()))

    def wait(self, run, *statuses, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = run.snapshot()
            if snap["status"] in statuses:
                return snap
            time.sleep(0.02)
        self.fail(f"run stuck in {run.snapshot()['status']}: {json.dumps(run.snapshot()['steps'], indent=1)[:3000]}")

    def step(self, snap, name):
        return next(s for s in snap["steps"] if s["name"] == name)


class FlowTests(Base):
    def test_plan_apply_wire_with_approvals(self):
        run = self.mgr.start(cfg(), steps=["plan", "apply", "wire"])
        snap = self.wait(run, "waiting_approval")
        self.assertEqual([s["name"] for s in snap["steps"]], STEPS)
        apply = self.step(snap, "apply")
        self.assertEqual(apply["status"], "needs_approval")
        plan = self.step(snap, "plan")["evidence"]["plan"]
        self.assertEqual(plan["summary"]["create"], 2)          # runtime + pull mappings
        self.assertEqual(apply["approval"]["plan_sha256"], plan["sha256"])

        with self.assertRaises(RunError) as cm:
            self.mgr.approve(run.id, "apply", {"plan_sha256": "0" * 64})
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "plan_hash_mismatch"))
        with self.assertRaises(RunError):
            self.mgr.approve(run.id, "wire", {"diff_sha256": "x"})      # not the step that is waiting
        self.assertFalse((self.ws() / ".fake_applied").exists())

        self.mgr.approve(run.id, "apply", {"plan_sha256": plan["sha256"]})
        snap = self.wait(run, "waiting_approval")
        wire = self.step(snap, "wire")
        self.assertEqual(self.step(snap, "apply")["status"], "succeeded")
        self.assertEqual(wire["status"], "needs_approval")
        self.assertTrue((self.ws() / ".fake_applied").exists())

        self.mgr.approve(run.id, "wire", {"diff_sha256": wire["approval"]["diff_sha256"]})
        snap = self.wait(run, "succeeded")
        self.assertTrue(self.step(snap, "wire")["evidence"]["result"]["verified"])
        for name in ("first_rotation", "verify", "report"):
            self.assertEqual(self.step(snap, name)["status"], "skipped")
        for name in ("preflight", "discover", "simulate"):
            self.assertEqual(self.step(snap, name)["status"], "skipped")

        events = (self.root / "runs" / run.id / "events.jsonl").read_text()
        self.assertIn('"approval_required"', events)
        self.assertNotIn(TOKEN, events)
        self.assertNotIn(TOKEN, json.dumps(run.snapshot()))

    def test_cancel_while_waiting(self):
        run = self.mgr.start(cfg(), steps=["plan", "apply"])
        self.wait(run, "waiting_approval")
        self.mgr.cancel(run.id)
        snap = self.wait(run, "cancelled")
        self.assertEqual(self.step(snap, "apply")["status"], "cancelled")
        self.assertFalse((self.ws() / ".fake_applied").exists())
        with self.assertRaises(RunError):
            self.mgr.approve(run.id, "apply", {"plan_sha256": "x"})

    def test_plan_failure_stops_run(self):
        ws = self.ws()
        ws.mkdir(parents=True)
        (ws / ".fake_fail_plan").touch()
        run = self.mgr.start(cfg(), steps=["plan", "apply", "wire"])
        snap = self.wait(run, "failed")
        self.assertEqual(self.step(snap, "plan")["status"], "failed")
        self.assertIn("fake plan failure", self.step(snap, "plan")["error"])
        self.assertEqual(self.step(snap, "apply")["status"], "pending")

    def test_destructive_plan_needs_typed_confirmation(self):
        ws = self.ws()
        ws.mkdir(parents=True)
        (ws / ".fake_plan.json").write_text(json.dumps([
            {"address": "module.jfrog_oidc.platform_oidc_identity_mapping.workload[\"old-runtime\"]",
             "type": "platform_oidc_identity_mapping", "change": {"actions": ["delete"]}}]))
        run = self.mgr.start(cfg(), steps=["plan", "apply"])
        snap = self.wait(run, "waiting_approval")
        approval = self.step(snap, "apply")["approval"]
        self.assertEqual(approval["confirmation_phrase"], ws.name)
        with self.assertRaises(RunError) as cm:
            self.mgr.approve(run.id, "apply", {"plan_sha256": approval["plan_sha256"]})
        self.assertEqual(cm.exception.code, "typed_confirmation_required")
        self.mgr.approve(run.id, "apply", {"plan_sha256": approval["plan_sha256"], "typed_confirmation": ws.name})
        self.wait(run, "succeeded")

    def test_no_change_plan_needs_no_approval(self):
        ws = self.ws()
        ws.mkdir(parents=True)
        (ws / ".fake_plan.json").write_text("[]")
        run = self.mgr.start(cfg(), steps=["plan", "apply"])
        snap = self.wait(run, "succeeded")
        self.assertEqual(self.step(snap, "apply")["evidence"]["result"]["applied"], False)

    def test_one_active_run_per_config(self):
        run = self.mgr.start(cfg(), steps=["plan", "apply"])
        self.wait(run, "waiting_approval")
        with self.assertRaises(RunError) as cm:
            self.mgr.start(cfg(), steps=["plan"])
        self.assertEqual(cm.exception.status, 409)
        self.mgr.cancel(run.id)

    def test_unknown_steps_rejected(self):
        with self.assertRaises(RunError):
            self.mgr.start(cfg(), steps=["plan", "rm -rf"])


class PreflightGateTests(Base):
    cloud_fixtures = load_fixtures(FX / "preflight" / "gcp_expired_login.json")

    def test_failed_preflight_stops_before_plan(self):
        run = self.mgr.start(cfg(), steps=["preflight", "plan"])
        snap = self.wait(run, "failed")
        self.assertEqual(self.step(snap, "preflight")["status"], "failed")
        self.assertEqual(self.step(snap, "plan")["status"], "pending")
        self.assertFalse(self.ws().exists())


if __name__ == "__main__":
    unittest.main()


class RestoreTests(Base):
    """Runs survive a console restart: the list and each run's steps and log come back from disk."""

    def fresh(self):
        mgr = RunManager(self.root, cloud_shell=None, tf_shell=None, env={})
        self.addCleanup(mgr.shutdown)
        return mgr

    def test_finished_run_comes_back(self):
        run = self.mgr.start(cfg(), steps=["plan", "apply", "wire"])
        snap = self.wait(run, "waiting_approval")
        self.mgr.cancel(run.id)
        before = self.wait(run, "cancelled")
        mgr = self.fresh()
        self.assertEqual([r["id"] for r in mgr.list()], [run.id])
        back = mgr.get(run.id)
        self.assertEqual(back.summary(), run.summary())
        self.assertEqual(back.snapshot()["steps"], before["steps"])
        self.assertEqual(len(back.events), len(run.events))
        self.assertIsNone(back.waiting)
        del snap

    def test_run_cut_off_by_a_restart_is_cancelled(self):
        d = self.root / "runs" / "20261030-120000-abcdef"
        d.mkdir(parents=True)
        lines = [{"seq": 0, "t": "2026-10-30T12:00:00Z", "kind": "step", "step": "plan", "name": "plan", "status": "running", "error": None, "note": None},
                 {"seq": 1, "t": "2026-10-30T12:00:01Z", "kind": "log", "step": "plan", "line": "terraform plan → exit 0 (1.0s)"},
                 {"seq": 2, "t": "2026-10-30T12:00:02Z", "kind": "step", "step": "plan", "name": "plan", "status": "succeeded", "error": None, "note": None},
                 {"seq": 3, "t": "2026-10-30T12:00:02Z", "kind": "step", "step": "apply", "name": "apply", "status": "needs_approval", "error": None, "note": None}]
        (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines))
        (d / "config.json").write_text(json.dumps({"name": "orders", "target": {"kind": "gcp_run_service"}}))
        back = self.fresh().get(d.name)
        snap = back.snapshot()
        self.assertEqual((snap["status"], snap["name"], snap["kind"], snap["created"]),
                         ("cancelled", "orders", "gcp_run_service", "2026-10-30T12:00:00Z"))
        self.assertEqual(self.step(snap, "plan")["status"], "succeeded")
        self.assertEqual(self.step(snap, "apply")["status"], "cancelled")
        self.assertIn("restarted", self.step(snap, "apply")["note"])

    def test_unreadable_run_folder_is_skipped(self):
        (self.root / "runs" / "junk").mkdir(parents=True)
        (self.root / "runs" / "20261030-130000-badbad").mkdir(parents=True)
        (self.root / "runs" / "20261030-130000-badbad" / "run.json").write_text("{not json")
        self.assertEqual(self.fresh().list(), [])
