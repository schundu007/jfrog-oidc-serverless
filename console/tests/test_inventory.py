"""Inventory: defaults and workload lists from replayed gcloud/az output."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from console import inventory
from console.shell import Result, Shell

SUB = "00000000-0000-0000-0000-000000000001"
SUB2 = "00000000-0000-0000-0000-000000000002"
OUT = {
    ("gcloud", "config", "get-value", "project"): "demo-nonprod-1234\n",
    ("gcloud", "config", "get-value", "run/region"): "\n",
    ("gcloud", "run", "services", "list"): json.dumps([
        {"metadata": {"name": "orders", "labels": {"cloud.googleapis.com/location": "europe-west1"}}},
        {"metadata": {"name": "resize", "labels": {"goog-managed-by": "cloudfunctions",
                                                   "cloud.googleapis.com/location": "us-central1"}}}]),
    ("gcloud", "run", "jobs", "list"): json.dumps([
        {"metadata": {"name": "nightly", "labels": {"cloud.googleapis.com/location": "us-central1"}}}]),
    ("gcloud", "projects", "list"): json.dumps([
        {"projectId": "demo-nonprod-1234", "name": "Demo", "lifecycleState": "ACTIVE"},
        {"projectId": "legacy-apps-5678", "name": "Legacy", "lifecycleState": "ACTIVE"},
        {"projectId": "gone-0000", "lifecycleState": "DELETE_REQUESTED"}]),
    ("az", "account", "show"): json.dumps({"id": SUB, "name": "nonprod"}),
    ("az", "resource", "list"): "2",
    ("az", "account", "list"): json.dumps([{"id": SUB, "name": "nonprod", "state": "Enabled"},
                                           {"id": SUB2, "name": "legacy", "state": "Enabled"},
                                           {"id": "x", "name": "off", "state": "Disabled"},
                                           {"id": "t-1", "tenantId": "t-1", "name": "N/A(tenant level account)",
                                            "state": "Enabled"}]),
    ("az", "containerapp", "list"): json.dumps([
        {"name": "api", "resourceGroup": "rg-api", "location": "West US 2", "kind": None},
        {"name": "fn", "resourceGroup": "rg-fn", "location": "West US 2", "kind": "functionapp"}]),
    ("az", "containerapp", "job", "list"): json.dumps([{"name": "caj-x", "resourceGroup": "rg-x", "location": "West US 2"}]),
    ("az", "functionapp", "list"): json.dumps([
        {"name": "flex", "resourceGroup": "rg-f", "location": "eastus", "sku": "FlexConsumption"},
        {"name": "prem", "resourceGroup": "rg-p", "location": "eastus", "sku": "ElasticPremium"}]),
}


class Fake(Shell):
    def __init__(self, fail=()):
        super().__init__(mode="replay")
        self.fail, self.calls = set(fail), []

    def _replay_cmd(self, argv):
        self.calls.append(list(argv))
        for n in (5, 4, 3):
            key = tuple(argv[:n])
            if key in OUT:
                if key in self.fail:
                    return Result(argv, 1, "", "ERROR: denied", 0.0)
                return Result(argv, 0, OUT[key], "", 0.0)
        self.misses.append(" ".join(argv))
        return Result(argv, 127, "", "no fixture", 0.0)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="inv-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "pipelines" / "vars").mkdir(parents=True)
        (self.repo / "pipelines" / "vars" / "test.yml").write_text(
            "variables:\n  JFROG_URL: https://acme.jfrog.io/\n")

    def test_gcp(self):
        sh = Fake()
        res = inventory.run("gcp", sh, self.repo)
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["defaults"], {"project": "demo-nonprod-1234", "region": "us-central1",
                                           "jfrog_url": "https://acme.jfrog.io"})
        self.assertEqual([(w["kind"], w["name"], w["region"]) for w in res["workloads"]], [
            ("gcp_run_function", "resize", "us-central1"), ("gcp_run_job", "nightly", "us-central1"),
            ("gcp_run_service", "orders", "europe-west1")])

    def test_azure_lists_every_kind(self):
        res = inventory.run("azure", Fake(), self.repo)
        self.assertEqual(res["defaults"]["subscription"], SUB)
        kinds = {w["name"]: (w["kind"], w["supported"], w["resource_group"]) for w in res["workloads"]}
        self.assertEqual(kinds, {
            "api": ("azure_container_app", True, "rg-api"), "fn": ("azure_functions_aca", True, "rg-fn"),
            "flex": ("azure_functions_flex", True, "rg-f"), "prem": ("azure_functions_premium", True, "rg-p"),
            "caj-x": ("azure_container_app_job", True, "rg-x")})

    def test_list_failure_is_reported_not_raised(self):
        res = inventory.run("gcp", Fake(fail={("gcloud", "run", "jobs", "list")}), self.repo)
        self.assertEqual([w["name"] for w in res["workloads"]], ["resize", "orders"])
        self.assertEqual(len(res["errors"]), 1)

    def test_scopes_and_picking_another_project(self):
        sh = Fake()
        res = inventory.run("gcp", sh, self.repo, scope="legacy-apps-5678")
        self.assertEqual([p["id"] for p in res["scopes"]], ["demo-nonprod-1234", "legacy-apps-5678"])
        self.assertEqual(res["defaults"]["project"], "legacy-apps-5678")
        self.assertIn(["gcloud", "run", "jobs", "list", "--project", "legacy-apps-5678", "--format=json"], sh.calls)
        self.assertNotIn(["gcloud", "config", "get-value", "project"], sh.calls)

    def test_azure_scopes_and_picking_another_subscription(self):
        sh = Fake()
        res = inventory.run("azure", sh, self.repo, scope=SUB2)
        self.assertEqual([s["name"] for s in res["scopes"]], ["legacy", "nonprod"])
        self.assertIn(["az", "account", "show", "--subscription", SUB2, "-o", "json"], sh.calls)

    def test_scopes_without_workloads_are_hidden_unless_picked(self):
        class Empty(Fake):
            def _replay_cmd(self, argv):
                if "legacy-apps-5678" in argv and argv[:2] == ["gcloud", "run"]:
                    self.calls.append(list(argv))
                    return Result(argv, 0, "[]", "", 0.0)
                if argv[:3] == ["az", "resource", "list"] and SUB2 in argv:
                    self.calls.append(list(argv))
                    return Result(argv, 0, "0", "", 0.0)
                return super()._replay_cmd(argv)
        self.assertEqual([p["id"] for p in inventory.run("gcp", Empty(), self.repo)["scopes"]], ["demo-nonprod-1234"])
        picked = inventory.run("gcp", Empty(), self.repo, scope="legacy-apps-5678")["scopes"]
        self.assertEqual([p["id"] for p in picked], ["demo-nonprod-1234", "legacy-apps-5678"])
        self.assertEqual([s["name"] for s in inventory.run("azure", Empty(), self.repo)["scopes"]], ["nonprod"])

    def test_bad_scope_is_refused_before_any_command(self):
        sh = Fake()
        res = inventory.run("gcp", sh, self.repo, scope="--impersonate-service-account=x")
        self.assertEqual(sh.calls, [])
        self.assertTrue(res["errors"])

    def test_onboarded_from_saved_and_delivered_configs(self):
        saved = {"target": {"kind": "gcp_run_job", "gcp": {"project": "demo-nonprod-1234", "region": "us-central1",
                                                            "name": "nightly"}}}
        ws = self.repo / "terraform" / "workspaces" / "gcp_demo" / "configs"
        ws.mkdir(parents=True)
        (ws / "orders.json").write_text(json.dumps({"target": {"kind": "gcp_run_service", "gcp": {
            "project": "demo-nonprod-1234", "region": "europe-west1", "name": "orders"}}}))
        res = inventory.run("gcp", Fake(), self.repo, configs=[saved])
        self.assertEqual({w["name"]: w["onboarded"] for w in res["workloads"]},
                         {"resize": False, "nightly": True, "orders": True})
        self.assertEqual(res["workloads"][0]["name"], "resize")   # not yet onboarded first

    def test_other_tenant_login_failure_is_one_clear_line(self):
        class AuthFail(Fake):
            def _replay_cmd(self, argv):
                if argv[:2] == ["az", "containerapp"] or argv[:2] == ["az", "functionapp"]:
                    self.calls.append(list(argv))
                    return Result(argv, 1, "", "ERROR: AADSTS7000215: Invalid client secret provided.\n"
                                               "The error may be caused by passing a certificate with --password.", 0.0)
                return super()._replay_cmd(argv)
        sh = AuthFail()
        res = inventory.run("azure", sh, self.repo)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("AADSTS7000215", res["errors"][0])
        self.assertIn("az login --tenant", res["errors"][0])
        self.assertEqual(len([c for c in sh.calls if c[1] in ("containerapp", "functionapp")]), 1)

    def test_cloud_run_disabled_is_one_line(self):
        class Disabled(Fake):
            def _replay_cmd(self, argv):
                if argv[:2] == ["gcloud", "run"]:
                    self.calls.append(list(argv))
                    return Result(argv, 1, "", "ERROR: (gcloud.run.services.list) PERMISSION_DENIED: Cloud Run Admin "
                                               "API has not been used in project p before or it is disabled.", 0.0)
                return super()._replay_cmd(argv)
        res = inventory.run("gcp", Disabled(), self.repo)
        self.assertEqual(res["errors"], ["Cloud Run is not enabled in demo-nonprod-1234: nothing to onboard there"])

    def test_cache_persists_and_skips_the_cloud(self):
        path = self.repo / "inventory.json"
        raw = inventory.fetch("gcp", Fake(), "")
        inventory.Cache(path).put("gcp", "", raw)
        cache = inventory.Cache(path)
        hit = cache.get("gcp", "")
        self.assertEqual([w["name"] for w in hit["workloads"]], [w["name"] for w in raw["workloads"]])
        # The CLI default is also the scope it resolved to.
        self.assertIsNotNone(cache.get("gcp", "demo-nonprod-1234"))
        self.assertIsNone(cache.get("gcp", "legacy-apps-5678"))
        self.assertIsNone(cache.get("azure", ""))
        self.assertIn("fetched_at", hit)

    def test_cache_skips_results_with_errors(self):
        path = self.repo / "inventory.json"
        inventory.Cache(path).put("gcp", "", inventory.fetch("gcp", Fake(fail={("gcloud", "run", "jobs", "list")}), ""))
        self.assertIsNone(inventory.Cache(path).get("gcp", ""))

    def test_cached_workloads_are_marked_onboarded_fresh(self):
        raw = inventory.fetch("gcp", Fake(), "")
        saved = {"target": {"kind": "gcp_run_job", "gcp": {"project": "demo-nonprod-1234", "region": "us-central1",
                                                           "name": "nightly"}}}
        before = inventory.annotate("gcp", raw, self.repo)
        after = inventory.annotate("gcp", raw, self.repo, configs=[saved])
        self.assertFalse(any(w["onboarded"] for w in before["workloads"]))
        self.assertEqual([w["name"] for w in after["workloads"] if w["onboarded"]], ["nightly"])
        self.assertEqual(after["defaults"]["jfrog_url"], "https://acme.jfrog.io")

    def test_only_read_commands(self):
        sh = Fake()
        inventory.run("gcp", sh, self.repo)
        inventory.run("azure", sh, self.repo)
        writes = {"create", "update", "delete", "set", "deploy", "execute", "start", "add", "remove"}
        self.assertFalse([c for c in sh.calls if writes & set(c)])


if __name__ == "__main__":
    unittest.main()
