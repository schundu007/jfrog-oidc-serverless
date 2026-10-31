"""Discover against recorded fixtures: all 7 kinds, kind corrections, identity edge cases."""

import json
import unittest
from pathlib import Path

from console import model
from console.shell import Shell
from console.steps import discover

FX = Path(__file__).parent / "fixtures" / "discover"
SUB = "11111111-2222-3333-4444-555555555555"
UAMI = f"/subscriptions/{SUB}/resourceGroups/rg-apps/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-orders"


def gcp_cfg(kind, name):
    cfg = model.default_config(kind, name="t")
    cfg["target"]["gcp"].update(project="demo-nonprod-1234", region="us-central1", name=name)
    return cfg


def azure_cfg(kind, name):
    cfg = model.default_config(kind, name="t")
    cfg["target"]["azure"].update(subscription=SUB, resource_group="rg-apps", name=name)
    return cfg


def run(cfg, fixture):
    sh = Shell.replay(FX / f"{fixture}.json")
    res = discover.run(cfg, sh)
    return res, sh


class AllKindsTests(unittest.TestCase):
    """One happy-path case per target kind (M3 acceptance)."""

    def check(self, cfg, fixture, kind, subject):
        res, sh = run(cfg, fixture)
        self.assertEqual(sh.misses, [], fixture)
        self.assertEqual(res["errors"], [], fixture)
        self.assertEqual(res["kind"], kind)
        self.assertEqual(res["facts"]["mapping_subject"], subject)
        self.assertEqual(res["config_patch"]["target.kind"], kind)
        self.assertEqual(res["config_patch"]["target.discovered"]["mapping_subject"], subject)
        return res

    def test_gcp_run_service(self):
        res = self.check(gcp_cfg("gcp_run_service", "orders"), "gcp_run_service", "gcp_run_service", "104857600000000000001")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["facts"]["image"].endswith("orders:1.0"))

    def test_gcp_run_job(self):
        res = self.check(gcp_cfg("gcp_run_job", "nightly"), "gcp_run_job", "gcp_run_job", "104857600000000000002")
        self.assertEqual(res["facts"]["service_account"], "nightly-batch@demo-nonprod-1234.iam.gserviceaccount.com")

    def test_gcp_run_function(self):
        res = self.check(gcp_cfg("gcp_run_function", "webhook"), "gcp_run_function", "gcp_run_function", "104857600000000000003")
        self.assertEqual(res["facts"]["run_service"], "webhook")
        self.assertEqual(res["facts"]["build_service_account"], "builder@demo-nonprod-1234.iam.gserviceaccount.com")

    def test_azure_container_app(self):
        res = self.check(azure_cfg("azure_container_app", "orders"), "azure_container_app", "azure_container_app",
                         "aaaaaaaa-0000-0000-0000-000000000001")
        self.assertEqual(res["facts"]["client_id"], "cccccccc-0000-0000-0000-000000000001")
        self.assertEqual(res["config_patch"]["target.azure.identity_type"], "user_assigned")
        self.assertTrue(res["config_patch"]["target.azure.environment_id"].endswith("/cae-dev"))

    def test_azure_functions_aca(self):
        res = self.check(azure_cfg("azure_functions_aca", "fn-aca"), "azure_functions_aca", "azure_functions_aca",
                         "aaaaaaaa-0000-0000-0000-000000000002")
        self.assertEqual(res["corrections"], [])

    def test_azure_functions_premium(self):
        res = self.check(azure_cfg("azure_functions_premium", "fn-prem"), "azure_functions_premium",
                         "azure_functions_premium", "aaaaaaaa-0000-0000-0000-000000000003")
        self.assertEqual(res["facts"]["plan_sku"], "EP1")
        self.assertEqual(res["facts"]["image"], "acme.jfrog.io/docker-virtual/fn:1.0")
        self.assertEqual(res["facts"]["identity_resource_id"], UAMI)

    def test_azure_functions_flex(self):
        res = self.check(azure_cfg("azure_functions_flex", "fn-flex"), "azure_functions_flex", "azure_functions_flex",
                         "aaaaaaaa-0000-0000-0000-000000000003")
        self.assertEqual(res["corrections"], [])  # default config already has the pull path off for Flex


class CorrectionTests(unittest.TestCase):
    def test_fc1_plan_corrects_premium_to_flex(self):
        res, _ = run(azure_cfg("azure_functions_premium", "fn-flex"), "azure_premium_is_flex")
        self.assertEqual(res["kind"], "azure_functions_flex")
        fields = {c["field"]: c for c in res["corrections"]}
        self.assertEqual(fields["target.kind"]["from"], "azure_functions_premium")
        self.assertIn("FC1", fields["target.kind"]["reason"])
        self.assertIs(fields["pull_path.enabled"]["to"], False)
        self.assertIs(res["config_patch"]["pull_path.enabled"], False)
        self.assertEqual(res["status"], "warn")

    def test_containerapp_with_functionapp_kind(self):
        res, _ = run(azure_cfg("azure_container_app", "fn-aca"), "azure_functions_aca")
        self.assertEqual(res["kind"], "azure_functions_aca")
        self.assertIn("kind=functionapp", res["corrections"][0]["reason"])

    def test_name_is_a_function_app_not_a_container_app(self):
        res, sh = run(azure_cfg("azure_container_app", "fn-code"), "azure_containerapp_is_functionapp")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["kind"], "azure_functions_premium")
        self.assertIn("code_deployment", {w["code"] for w in res["warnings"]})

    def test_service_type_on_a_job_is_corrected(self):
        res, sh = run(gcp_cfg("gcp_run_service", "nightly"), "gcp_service_is_job")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["kind"], "gcp_run_job")
        self.assertEqual(res["facts"]["mapping_subject"], "104857600000000000002")

    def test_cloud_run_service_managed_by_functions(self):
        res, sh = run(gcp_cfg("gcp_run_service", "webhook"), "gcp_service_is_function")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["kind"], "gcp_run_function")
        self.assertEqual(res["facts"]["mapping_subject"], "104857600000000000003")


class WarningAndErrorTests(unittest.TestCase):
    def test_default_compute_sa_gets_a_new_account_without_warning(self):
        res, sh = run(gcp_cfg("gcp_run_service", "orders"), "gcp_run_service_default_sa")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["facts"]["service_account"], "123456789012-compute@developer.gserviceaccount.com")
        self.assertTrue(res["facts"]["service_account_implicit"])
        self.assertNotIn("dedicated_identity", [w["code"] for w in res["warnings"]])
        self.assertEqual(res["status"], "ok")
        # Existing app on a default account: the workspace creates jfrog-<name>, no mapping on the default one.
        self.assertIsNone(res["facts"]["mapping_subject"])
        self.assertEqual(res["facts"]["dedicated_identity"], "jfrog-t")
        self.assertEqual(res["facts"]["replaced_service_account_roles"], ["roles/editor"])
        # Basic roles are never carried over to the new account.
        self.assertEqual(res["facts"]["dedicated_service_account_roles"], [])
        self.assertIn("target.discovered", res["config_patch"])

    def test_identity_created_earlier_is_kept(self):
        cfg = gcp_cfg("gcp_run_job", "nightly")
        cfg["name"] = "nightly"
        res, sh = run(cfg, "gcp_run_job_dedicated")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["facts"]["dedicated_identity"], "jfrog-nightly")
        self.assertEqual(res["facts"]["mapping_subject"], "104857600000000000002")

    def test_system_assigned_only_gets_a_dedicated_one(self):
        # A system identity dies with the app and probes cannot borrow it: plan a dedicated one.
        cfg = azure_cfg("azure_container_app", "sys")
        cfg["name"] = "sys"
        res, _ = run(cfg, "azure_container_app_system_only")
        self.assertEqual((res["status"], res["warnings"]), ("ok", []))
        self.assertEqual(res["facts"]["dedicated_identity"], "id-sys-jfrog")
        self.assertIsNone(res["facts"]["mapping_subject"])
        self.assertEqual(res["config_patch"]["target.azure.identity_type"], "user_assigned")

    def test_ambiguous_identity(self):
        res, _ = run(azure_cfg("azure_container_app", "multi"), "azure_container_app_ambiguous")
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["errors"][0]["code"], "ambiguous_identity")
        self.assertNotIn("target.kind", res["config_patch"])

    def test_ambiguous_identity_resolved_by_config(self):
        cfg = azure_cfg("azure_container_app", "multi")
        cfg["target"]["azure"]["identity_resource_id"] = UAMI.upper()
        res, _ = run(cfg, "azure_container_app_ambiguous")
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["facts"]["principal_id"], "p1")

    def test_no_identity_gets_a_dedicated_one(self):
        cfg = azure_cfg("azure_container_app", "noid")
        cfg["name"] = "noid"
        res, _ = run(cfg, "azure_container_app_no_identity")
        self.assertEqual((res["status"], res["warnings"]), ("ok", []))
        self.assertEqual(res["facts"]["dedicated_identity"], "id-noid-jfrog")
        self.assertIsNone(res["facts"]["mapping_subject"])
        self.assertEqual(res["config_patch"]["target.azure.identity_type"], "user_assigned")

    def test_dedicated_identity_assigned_earlier_is_kept(self):
        cfg = azure_cfg("azure_container_app", "noid")
        cfg["name"] = "noid"
        res, _ = run(cfg, "azure_container_app_dedicated")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["facts"]["dedicated_identity"], "id-noid-jfrog")
        self.assertEqual(res["facts"]["mapping_subject"], "pp-dedicated")
        self.assertEqual(res["facts"]["client_id"], "cc-dedicated")

    def test_consumption_plan_rejected(self):
        res, _ = run(azure_cfg("azure_functions_premium", "fn-y1"), "azure_consumption")
        self.assertEqual(res["errors"][0]["code"], "consumption_plan")

    def test_gen1_function_rejected(self):
        res, _ = run(gcp_cfg("gcp_run_function", "legacy"), "gcp_function_gen1")
        self.assertEqual(res["errors"][0]["code"], "gen1_function")

    def test_not_found(self):
        res, _ = run(gcp_cfg("gcp_run_service", "ghost"), "gcp_not_found")
        self.assertEqual(res["errors"][0]["code"], "not_found")
        res, sh = run(azure_cfg("azure_container_app", "ghost"), "azure_not_found")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["errors"][0]["code"], "not_found")

    def test_result_is_json_and_patch_applies(self):
        res, _ = run(azure_cfg("azure_functions_premium", "fn-flex"), "azure_premium_is_flex")
        json.dumps(res)
        cfg = azure_cfg("azure_functions_premium", "fn-flex")
        cfg["pull_path"]["enabled"] = True
        res, _ = run(cfg, "azure_premium_is_flex")
        for k, v in res["config_patch"].items():
            model.set_path(cfg, k, v)
        self.assertEqual(cfg["target"]["kind"], "azure_functions_flex")
        self.assertFalse(cfg["pull_path"]["enabled"])
        self.assertEqual(model.validate(cfg)["errors"], [])


if __name__ == "__main__":
    unittest.main()


class RealGcloudWordingTests(unittest.TestCase):
    """Error text as recorded from gcloud 555 against a real project (M3 live run)."""

    def test_cannot_find_service_is_not_found(self):
        res, _ = run(gcp_cfg("gcp_run_service", "ghost"), "real_gcp_wording")
        self.assertEqual([e["code"] for e in res["errors"]], ["not_found"])
        self.assertIn("Check the name, region and project", res["errors"][0]["fix"])

    def test_cannot_find_job_is_not_found(self):
        res, _ = run(gcp_cfg("gcp_run_job", "ghost"), "real_gcp_wording")
        self.assertEqual([e["code"] for e in res["errors"]], ["not_found"])

    def test_functions_api_disabled_falls_back_to_service_lookup(self):
        # No gen2 function can exist while the API is off; look for a Cloud Run
        # service by the name instead, and say why.
        res, sh = run(gcp_cfg("gcp_run_function", "ghost"), "real_gcp_wording")
        self.assertEqual(sh.misses, [])
        self.assertIn("functions_api_disabled", [w["code"] for w in res["warnings"]])
        self.assertIn("gcloud services enable cloudfunctions.googleapis.com --project demo-nonprod-1234",
                      [w for w in res["warnings"] if w["code"] == "functions_api_disabled"][0]["fix"])
        self.assertEqual([e["code"] for e in res["errors"]], ["not_found"])


class RealAzWordingTests(unittest.TestCase):
    """Error text as recorded from az 2.86 against a real subscription (M3 live run)."""

    def test_unregistered_provider_means_not_found_plus_warning(self):
        for kind in ("azure_functions_premium", "azure_container_app"):
            res, sh = run(azure_cfg(kind, "ghost"), "real_azure_wording")
            self.assertEqual(sh.misses, [], kind)
            self.assertEqual([e["code"] for e in res["errors"]], ["not_found"], kind)
            w = [w for w in res["warnings"] if w["code"] == "provider_not_registered"]
            self.assertEqual(len(w), 1, kind)
            self.assertIn(f"az provider register -n Microsoft.App --subscription {SUB} --wait", w[0]["fix"])
