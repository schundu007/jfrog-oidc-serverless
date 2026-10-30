"""Inventory: defaults and workload lists from replayed gcloud/az output."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from console import inventory
from console.shell import Result, Shell

SUB = "00000000-0000-0000-0000-000000000001"
OUT = {
    ("gcloud", "config", "get-value", "project"): "demo-nonprod-1234\n",
    ("gcloud", "config", "get-value", "run/region"): "\n",
    ("gcloud", "run", "services", "list"): json.dumps([
        {"metadata": {"name": "orders", "labels": {"cloud.googleapis.com/location": "europe-west1"}}},
        {"metadata": {"name": "resize", "labels": {"goog-managed-by": "cloudfunctions",
                                                   "cloud.googleapis.com/location": "us-central1"}}}]),
    ("gcloud", "run", "jobs", "list"): json.dumps([
        {"metadata": {"name": "nightly", "labels": {"cloud.googleapis.com/location": "us-central1"}}}]),
    ("az", "account", "show"): json.dumps({"id": SUB, "name": "nonprod"}),
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

    def test_azure_marks_jobs_unsupported(self):
        res = inventory.run("azure", Fake(), self.repo)
        self.assertEqual(res["defaults"]["subscription"], SUB)
        kinds = {w["name"]: (w["kind"], w["supported"], w["resource_group"]) for w in res["workloads"]}
        self.assertEqual(kinds, {
            "api": ("azure_container_app", True, "rg-api"), "fn": ("azure_functions_aca", True, "rg-fn"),
            "flex": ("azure_functions_flex", True, "rg-f"), "prem": ("azure_functions_premium", True, "rg-p"),
            "caj-x": ("azure_container_app_job", False, "rg-x")})
        self.assertEqual(res["workloads"][-1]["name"], "caj-x")

    def test_list_failure_is_reported_not_raised(self):
        res = inventory.run("gcp", Fake(fail={("gcloud", "run", "jobs", "list")}), self.repo)
        self.assertEqual([w["name"] for w in res["workloads"]], ["resize", "orders"])
        self.assertEqual(len(res["errors"]), 1)

    def test_only_read_commands(self):
        sh = Fake()
        inventory.run("gcp", sh, self.repo)
        inventory.run("azure", sh, self.repo)
        writes = {"create", "update", "delete", "set", "deploy", "execute", "start", "add", "remove"}
        self.assertFalse([c for c in sh.calls if writes & set(c)])


if __name__ == "__main__":
    unittest.main()
