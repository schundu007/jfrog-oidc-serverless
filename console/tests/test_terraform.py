"""terraform.py against a fake terraform binary: plan summary and the apply gates (spec §4.3, M4)."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from console.shell import Shell
from console.steps import terraform as tf

FAKE = Path(__file__).parent / "fixtures" / "fake_terraform"


def change(address, *actions, type_="platform_oidc_identity_mapping"):
    return {"address": address, "type": type_, "change": {"actions": list(actions)}}


CREATE_ONLY = [change("module.jfrog_oidc.platform_oidc_identity_mapping.workload[\"orders-runtime\"]", "create"),
               change("module.jfrog_oidc.platform_oidc_configuration.gcp[0]", "create", type_="platform_oidc_configuration"),
               change("module.gcp_pull_path[0].google_secret_manager_secret.upstream", "no-op",
                      type_="google_secret_manager_secret")]
WITH_REPLACE = CREATE_ONLY + [change("module.gcp_pull_path[0].google_cloud_run_v2_job.rotator", "delete", "create",
                                     type_="google_cloud_run_v2_job")]
WITH_DELETE = CREATE_ONLY + [change("module.jfrog_oidc.platform_oidc_identity_mapping.workload[\"old-runtime\"]", "delete")]


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="tfws-")) / "gcp_demo-nonprod-1234_us-central1"
        self.ws.mkdir()
        self.addCleanup(shutil.rmtree, self.ws.parent, True)
        self.lines = []
        sh = Shell(mode="live", log=self.lines.append, which=lambda n: str(FAKE) if n == "terraform" else None)
        self.tf = tf.Terraform(sh, self.ws, env={"JFROG_ACCESS_TOKEN": "test-admin-token-value", "PATH": "/usr/bin:/bin"})

    def set_plan(self, changes):
        (self.ws / ".fake_plan.json").write_text(json.dumps(changes))

    def calls(self):
        p = self.ws / ".fake_calls"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def applied(self):
        return (self.ws / ".fake_applied").exists()


class PlanTests(Base):
    def test_plan_summary_and_hash(self):
        self.set_plan(CREATE_ONLY)
        plan = self.tf.plan()
        self.assertEqual(plan["summary"], {"import": 0, "create": 2, "update": 0, "replace": 0, "delete": 0, "read": 0, "no-op": 1})
        self.assertEqual(plan["destructive"], [])
        self.assertEqual(plan["sha256"], hashlib.sha256((self.ws / "plan.bin").read_bytes()).hexdigest())
        self.assertIsNone(plan["confirmation_phrase"])
        argv = [c["argv"] for c in self.calls()]
        self.assertEqual(argv[0][0], "init")
        self.assertIn(["plan", "-out=plan.bin", "-input=false", "-lock-timeout=60s", "-no-color", "-detailed-exitcode"], argv)
        self.assertIn(["show", "-json", "-no-color", "plan.bin"], argv)

    def test_replace_and_delete_are_destructive(self):
        self.set_plan(WITH_REPLACE)
        plan = self.tf.plan()
        self.assertEqual(plan["summary"]["replace"], 1)
        self.assertEqual(plan["destructive"], ["module.gcp_pull_path[0].google_cloud_run_v2_job.rotator"])
        self.assertEqual(plan["confirmation_phrase"], "gcp_demo-nonprod-1234_us-central1")

    def test_plan_failure_reported(self):
        self.set_plan(CREATE_ONLY)
        (self.ws / ".fake_fail_plan").touch()
        with self.assertRaisesRegex(tf.TerraformError, "fake plan failure"):
            self.tf.plan()

    def test_token_reaches_terraform_only_through_env(self):
        self.set_plan(CREATE_ONLY)
        self.tf.plan()
        self.assertTrue(all(c["jfrog_token_env"] for c in self.calls()))
        self.assertTrue(all("test-admin-token-value" not in a for c in self.calls() for a in c["argv"]))
        self.assertNotIn("test-admin-token-value", "\n".join(self.lines))


class ApplyGateTests(Base):
    def test_refused_without_approval(self):
        self.set_plan(CREATE_ONLY)
        plan = self.tf.plan()
        for approval in (None, {}, {"plan_sha256": ""}):
            with self.assertRaises(tf.ApplyRefused) as cm:
                self.tf.apply(plan, approval)
            self.assertEqual(cm.exception.code, "not_approved")
        self.assertFalse(self.applied())

    def test_refused_on_hash_mismatch(self):
        self.set_plan(CREATE_ONLY)
        plan = self.tf.plan()
        with self.assertRaises(tf.ApplyRefused) as cm:
            self.tf.apply(plan, {"plan_sha256": "0" * 64})
        self.assertEqual(cm.exception.code, "plan_hash_mismatch")
        self.assertFalse(self.applied())

    def test_refused_when_plan_file_changed_after_approval(self):
        self.set_plan(CREATE_ONLY)
        plan = self.tf.plan()
        (self.ws / "plan.bin").write_text("tampered")
        with self.assertRaises(tf.ApplyRefused) as cm:
            self.tf.apply(plan, {"plan_sha256": plan["sha256"]})
        self.assertEqual(cm.exception.code, "plan_file_changed")
        self.assertFalse(self.applied())

    def test_refused_on_delete_without_typed_confirmation(self):
        for changes in (WITH_DELETE, WITH_REPLACE):
            self.set_plan(changes)
            plan = self.tf.plan()
            for typed in (None, "", "yes", "gcp_demo-nonprod-1234"):
                with self.assertRaises(tf.ApplyRefused) as cm:
                    self.tf.apply(plan, {"plan_sha256": plan["sha256"], "typed_confirmation": typed})
                self.assertEqual(cm.exception.code, "typed_confirmation_required")
            self.assertFalse(self.applied())

    def test_destructive_apply_with_typed_confirmation(self):
        self.set_plan(WITH_DELETE)
        plan = self.tf.plan()
        res = self.tf.apply(plan, {"plan_sha256": plan["sha256"], "typed_confirmation": plan["confirmation_phrase"]})
        self.assertTrue(self.applied())
        self.assertTrue(res["applied"])

    def test_apply_uses_only_the_plan_file(self):
        self.set_plan(CREATE_ONLY)
        (self.ws / ".fake_outputs.json").write_text(json.dumps({"jfrog_provider_name": {"value": "gcp-cloudrun", "sensitive": False}}))
        plan = self.tf.plan()
        res = self.tf.apply(plan, {"plan_sha256": plan["sha256"]})
        apply_calls = [c["argv"] for c in self.calls() if c["argv"][0] == "apply"]
        self.assertEqual(apply_calls, [["apply", "-input=false", "-lock-timeout=60s", "-no-color", "plan.bin"]])
        self.assertEqual(res["outputs"], {"jfrog_provider_name": "gcp-cloudrun"})
        # a plan file is single-use
        self.assertFalse((self.ws / "plan.bin").exists())
        with self.assertRaises(tf.ApplyRefused):
            self.tf.apply(plan, {"plan_sha256": plan["sha256"]})

    def test_sensitive_outputs_never_returned(self):
        (self.ws / ".fake_outputs.json").write_text(json.dumps({"a": {"value": "x", "sensitive": False},
                                                                "b": {"value": "secret-ish", "sensitive": True}}))
        self.assertEqual(self.tf.outputs(), {"a": "x", "b": "[sensitive]"})

    def test_apply_failure_reported(self):
        self.set_plan(CREATE_ONLY)
        plan = self.tf.plan()
        (self.ws / ".fake_fail_apply").touch()
        with self.assertRaisesRegex(tf.TerraformError, "fake apply failure"):
            self.tf.apply(plan, {"plan_sha256": plan["sha256"]})


class LockTests(Base):
    def test_apply_waits_for_workspace_lock(self):
        from console.steps.tfgen import terraform_lock
        self.set_plan(CREATE_ONLY)
        plan = self.tf.plan()
        waits = []
        self.tf.on_wait = waits.append
        self.tf.lock_timeout = 0.3
        with terraform_lock(self.ws):
            with self.assertRaises(TimeoutError):
                self.tf.apply(plan, {"plan_sha256": plan["sha256"]})
        self.assertTrue(waits)
        self.assertFalse(self.applied())


if __name__ == "__main__":
    unittest.main()


class SummaryTests(unittest.TestCase):
    def test_import_counted_and_listed(self):
        res = tf.summarise([
            {"address": "module.jfrog_oidc.platform_oidc_configuration.gcp[0]", "type": "platform_oidc_configuration",
             "change": {"actions": ["no-op"], "importing": {"id": "gcp-cloudrun"}}},
            {"address": "module.jfrog_oidc.platform_oidc_identity_mapping.workload[\"a\"]",
             "type": "platform_oidc_identity_mapping", "change": {"actions": ["create"]}},
        ])
        self.assertEqual(res["summary"]["import"], 1)
        self.assertEqual(res["summary"]["create"], 1)
        self.assertEqual(res["summary"]["no-op"], 0)
        self.assertEqual([c["action"] for c in res["changes"]], ["import", "create"])
        self.assertEqual(res["destructive"], [])
