"""Preflight against recorded fixtures (synthetic accounts; nothing runs for real)."""

import json
import unittest
from pathlib import Path

from console import model
from console.shell import Shell, load_fixtures
from console.steps import preflight

FX = Path(__file__).parent / "fixtures" / "preflight"
SUB = "11111111-2222-3333-4444-555555555555"
KV = f"/subscriptions/{SUB}/resourceGroups/rg-apps/providers/Microsoft.KeyVault/vaults/kv-demo"
TOKEN_ENV = {"JFROG_ACCESS_TOKEN": "test-admin-token-value"}


def gcp_cfg(kind="gcp_run_service"):
    cfg = model.default_config(kind, name="orders")
    cfg["target"]["gcp"].update(project="demo-nonprod-1234", region="us-central1", name="orders")
    return cfg


def azure_cfg(kind="azure_container_app"):
    cfg = model.default_config(kind, name="orders")
    cfg["target"]["azure"].update(subscription=SUB, resource_group="rg-apps", name="orders", key_vault_id=KV)
    return cfg


def by_id(result):
    return {c["id"]: c for c in result["checks"]}


class GcpPreflightTests(unittest.TestCase):
    def _shell(self, granted):
        fixtures = load_fixtures(FX / "gcp_ok.json")
        fixtures.append({"http": "POST https://cloudresourcemanager.googleapis.com/v1/projects/demo-nonprod-1234:testIamPermissions",
                         "status": 200, "body": json.dumps({"permissions": granted})})
        return Shell(mode="replay", fixtures=fixtures)

    def test_all_green(self):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        sh = self._shell(perms)
        res = preflight.run(gcp_cfg(), sh, env=TOKEN_ENV)
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["status"], "ok", [c for c in res["checks"] if c["status"] != "ok"])
        checks = by_id(res)
        self.assertEqual(checks["tool_terraform"]["detail"], "terraform 1.9.8")
        self.assertEqual(checks["jfrog_integration"]["detail"], "will be created")
        self.assertEqual(res["facts"]["gcp_project_number"], "123456789012")

    def test_existing_integration_same_issuer_is_adopted(self):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        sh = self._shell(perms)
        for f in sh.fixtures:
            if f.get("http") == "GET https://acme.jfrog.io/access/api/v1/oidc":
                f["body"] = json.dumps([{"name": "gcp-cloudrun", "issuer_url": "https://accounts.google.com/"}])
        res = preflight.run(gcp_cfg(), sh, env=TOKEN_ENV)
        check = by_id(res)["jfrog_integration"]
        self.assertEqual(check["status"], "ok")
        self.assertIn("reused", check["detail"])
        self.assertEqual(res["config_patch"], {"jfrog.adopt_integration": True})

    def test_new_integration_is_not_adopted(self):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        res = preflight.run(gcp_cfg(), self._shell(perms), env=TOKEN_ENV)
        self.assertEqual(res["config_patch"], {"jfrog.adopt_integration": False})

    def test_missing_permissions_warn_not_fail(self):
        res = preflight.run(gcp_cfg(), self._shell(["run.services.get"]), env=TOKEN_ENV)
        perm = by_id(res)["gcp_permissions"]
        self.assertEqual(perm["status"], "warn")
        self.assertIn("run.services.update", perm["detail"])
        self.assertEqual(res["status"], "warn")

    def test_expired_login(self):
        res = preflight.run(gcp_cfg(), Shell.replay(FX / "gcp_expired_login.json"), env=TOKEN_ENV)
        login = by_id(res)["gcloud_login"]
        self.assertEqual(login["status"], "fail")
        self.assertIn("gcloud auth login", login["fix"])

    def test_missing_jfrog_token(self):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        res = preflight.run(gcp_cfg(), self._shell(perms), env={})
        self.assertEqual(by_id(res)["jfrog_token"]["status"], "fail")
        self.assertEqual(res["status"], "fail")

    def test_delivery_needs_no_local_jfrog_token(self):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        res = preflight.run(gcp_cfg(), self._shell(perms), env={}, delivery=True)
        ids = by_id(res)
        for local_only in ("jfrog_token", "tool_terraform", "gcloud_adc", "gcp_permissions"):
            self.assertNotIn(local_only, ids)
        self.assertNotEqual(res["status"], "fail")

    def _reach(self, members):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        sh = self._shell(perms)
        sh.fixtures.append({"argv": ["gcloud", "projects", "get-iam-policy", "demo-nonprod-1234", "--format=json"], "rc": 0,
                            "stdout": json.dumps({"bindings": [{"role": "roles/run.admin", "members": members}]})})
        settings = {"pipeline_identity": {"gcp": "ado-setup@ci-1234.iam.gserviceaccount.com"}}
        return by_id(preflight.run(gcp_cfg(), sh, env={}, delivery=settings))["pipeline_reach"]

    def test_delivery_checks_the_pipeline_reaches_an_existing_apps_project(self):
        self.assertEqual(self._reach(["serviceAccount:ado-setup@ci-1234.iam.gserviceaccount.com"])["status"], "ok")
        miss = self._reach(["user:someone@example.com"])
        self.assertEqual(miss["status"], "fail")
        self.assertIn("-foundation", miss["fix"])

    def _code(self, remote_console, remote_modules, local_static="s1"):
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_service"]
        sh = self._shell(perms)
        ado = {"organization": "org", "project": "proj", "repository": "repo", "target_branch": "main"}
        local = {"console": {"deliver.py": "aaa111", "static": local_static, "tests": "t1"},
                 "terraform/modules": {"jfrog-oidc": "bbb222"}}
        remote = {"console": remote_console, "terraform/modules": remote_modules}
        for path, entries in local.items():
            sh.fixtures.append({"argv": ["git", "-C", "/repo", "ls-tree", "HEAD", f"{path}/"], "rc": 0,
                                "stdout": "".join(f"040000 tree {sha}\t{path}/{n}\n" for n, sha in entries.items())})
        for path, entries in remote.items():
            sh.fixtures.append({"argv": ["az", "devops", "invoke", "--org", "https://dev.azure.com/org", "--area", "git",
                                         "--resource", "items", "--route-parameters", "project=proj", "repositoryId=repo",
                                         "--query-parameters", f"scopePath=/{path}", "recursionLevel=OneLevel",
                                         "versionDescriptor.version=main", "--api-version", "7.1", "-o", "json"], "rc": 0,
                                "stdout": json.dumps({"value": [{"path": f"/{path}", "objectId": "root"}] + [
                                    {"path": f"/{path}/{n}", "objectId": sha} for n, sha in entries.items()]})})
        res = preflight.run(gcp_cfg(), sh, env={}, delivery={"ado": ado}, repo_root="/repo")
        self.assertEqual(sh.misses, [])
        return by_id(res)["pipeline_code"]

    def test_delivery_checks_the_pipeline_runs_this_code(self):
        same = {"deliver.py": "aaa111", "static": "s1", "tests": "t1"}
        mods = {"jfrog-oidc": "bbb222"}
        self.assertEqual(self._code(same, mods)["status"], "ok")
        stale = self._code({**same, "deliver.py": "old999"}, mods)
        self.assertEqual(stale["status"], "fail")
        self.assertIn("console", stale["detail"])
        self.assertIn("git push", stale["fix"])
        self.assertEqual(self._code(same, {"jfrog-oidc": "old"})["status"], "fail")
        # The pipeline doesn't run the UI or the tests: a UI-only change still matches.
        self.assertEqual(self._code({**same, "static": "s0", "tests": "t0"}, mods, local_static="s2")["status"], "ok")

    def test_old_terraform(self):
        res = preflight.run(gcp_cfg(), Shell.replay(FX / "old_terraform.json"), env=TOKEN_ENV)
        self.assertEqual(by_id(res)["tool_terraform"]["status"], "fail")


class AzurePreflightTests(unittest.TestCase):
    def test_contributor_only_and_non_admin_token(self):
        sh = Shell.replay(FX / "azure_contributor_only.json")
        res = preflight.run(azure_cfg(), sh, env=TOKEN_ENV)
        self.assertEqual(sh.misses, [])
        checks = by_id(res)
        self.assertEqual(checks["azure_permissions"]["status"], "warn")
        self.assertIn("User Access Administrator", checks["azure_permissions"]["fix"])
        self.assertEqual(checks["azure_kv_permissions"]["status"], "warn")
        self.assertEqual(checks["jfrog_token"]["status"], "fail")
        self.assertIn("admin", checks["jfrog_token"]["detail"])
        self.assertIn("--subscription", checks["az_default_subscription"]["detail"])

    def test_delivery_checks_the_pipeline_reaches_the_resource_group(self):
        rg_id = f"/subscriptions/{SUB}/resourceGroups/rg-apps"
        for roles, want in ((["Contributor", "Role Based Access Control Administrator"], "ok"), (["Reader"], "fail")):
            fixtures = load_fixtures(FX / "azure_contributor_only.json")
            fixtures.insert(0, {"argv": ["az", "role", "assignment", "list", "--assignee", "pid-setup", "--scope", rg_id,
                                         "--include-inherited", "--subscription", SUB, "-o", "json"], "rc": 0,
                                "stdout": json.dumps([{"roleDefinitionName": r} for r in roles])})
            res = preflight.run(azure_cfg(), Shell(mode="replay", fixtures=fixtures), env={},
                                delivery={"pipeline_identity": {"azure": "pid-setup"}})
            self.assertEqual(by_id(res)["pipeline_reach"]["status"], want, roles)

    def test_prod_subscription_existing_integration_missing_group(self):
        res = preflight.run(azure_cfg(), Shell.replay(FX / "azure_prod.json"), env=TOKEN_ENV)
        checks = by_id(res)
        self.assertEqual(checks["azure_subscription_prod"]["status"], "warn")
        self.assertEqual(checks["azure_permissions"]["status"], "ok")
        self.assertEqual(checks["azure_kv_permissions"]["status"], "ok")
        self.assertEqual(checks["jfrog_integration"]["status"], "warn")
        self.assertEqual(checks["jfrog_group"]["status"], "fail")

    def test_integration_for_another_issuer_is_refused(self):
        # AKS pilot: a provider name that belonged to another cluster's issuer cost hours.
        fixtures = load_fixtures(FX / "azure_prod.json")
        for f in fixtures:
            if f.get("http") == "GET https://acme.jfrog.io/access/api/v1/oidc":
                f["body"] = json.dumps([{"name": "azure-entra",
                                         "issuer_url": "https://centralus.oic.prod-aks.azure.com/tenant/cluster/"}])
        res = preflight.run(azure_cfg(), Shell(mode="replay", fixtures=fixtures), env=TOKEN_ENV)
        check = by_id(res)["jfrog_integration"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("another issuer", check["detail"])

    def test_jfrog_url_with_path_fails(self):
        cfg = azure_cfg()
        cfg["jfrog"]["url"] = "https://acme.jfrog.io/artifactory"
        res = preflight.run(cfg, Shell.replay(FX / "azure_prod.json"), env=TOKEN_ENV)
        self.assertEqual(by_id(res)["jfrog_url"]["status"], "fail")

    def test_token_never_in_result(self):
        res = preflight.run(azure_cfg(), Shell.replay(FX / "azure_prod.json"), env=TOKEN_ENV)
        self.assertNotIn(TOKEN_ENV["JFROG_ACCESS_TOKEN"], json.dumps(res))


class ProdNameTests(unittest.TestCase):
    def test_prod_heuristic(self):
        for name in ("trk-mt-prod-sub", "acme-prd", "Production", "live-payments"):
            self.assertTrue(preflight.PROD_NAME.search(name), name)
        for name in ("trk-mt-nprd-sub", "demo-dev", "product-catalog", "csb-test-257615"):
            self.assertFalse(preflight.PROD_NAME.search(name), name)


if __name__ == "__main__":
    unittest.main()


class ProviderAndApiTests(unittest.TestCase):
    """Resource providers / APIs the target needs: found missing in the M3 live run."""

    def _azure(self, unregistered, kind="azure_container_app"):
        fixtures = [f for f in load_fixtures(FX / "azure_contributor_only.json") if f.get("argv", [])[:3] != ["az", "provider", "list"]]
        states = [{"namespace": n, "state": "NotRegistered" if n in unregistered else "Registered"}
                  for n in ("Microsoft.App", "Microsoft.Web", "Microsoft.KeyVault", "Microsoft.ManagedIdentity")]
        fixtures.append({"argv": ["az", "provider", "list", "--subscription", SUB, "--query",
                                  "[].{namespace:namespace,state:registrationState}", "-o", "json"],
                         "rc": 0, "stdout": json.dumps(states)})
        sh = Shell(mode="replay", fixtures=fixtures)
        return by_id(preflight.run(azure_cfg(kind), sh, env=TOKEN_ENV)), sh

    def test_registered_providers_ok(self):
        checks, sh = self._azure(set())
        self.assertEqual(sh.misses, [])
        self.assertEqual(checks["azure_providers"]["status"], "ok")

    def test_unregistered_provider_fails_with_register_command(self):
        checks, _ = self._azure({"Microsoft.App"})
        c = checks["azure_providers"]
        self.assertEqual(c["status"], "fail")
        self.assertIn("Microsoft.App", c["detail"])
        self.assertIn(f"az provider register -n Microsoft.App --subscription {SUB} --wait", c["fix"])

    def test_flex_needs_web_not_app_without_pull_path(self):
        cfg_kind = "azure_functions_flex"   # pull path off by default for Flex
        checks, _ = self._azure({"Microsoft.App"}, kind=cfg_kind)
        self.assertEqual(checks["azure_providers"]["status"], "ok")
        checks, _ = self._azure({"Microsoft.Web"}, kind=cfg_kind)
        self.assertEqual(checks["azure_providers"]["status"], "fail")

    def test_gcp_function_needs_cloudfunctions_api(self):
        fixtures = [f for f in load_fixtures(FX / "gcp_ok.json") if f.get("argv", [])[:3] != ["gcloud", "services", "list"]]
        fixtures.append({"argv": ["gcloud", "services", "list", "--enabled", "--project", "demo-nonprod-1234", "--format=json"],
                         "rc": 0, "stdout": json.dumps([{"config": {"name": "run.googleapis.com"}}])})
        perms = preflight.GCP_PERMISSIONS_COMMON + preflight.GCP_PERMISSIONS_BY_KIND["gcp_run_function"]
        fixtures.append({"http": "POST https://cloudresourcemanager.googleapis.com/v1/projects/demo-nonprod-1234:testIamPermissions",
                         "status": 200, "body": json.dumps({"permissions": perms})})
        sh = Shell(mode="replay", fixtures=fixtures)
        c = by_id(preflight.run(gcp_cfg("gcp_run_function"), sh, env=TOKEN_ENV))["gcp_apis"]
        self.assertEqual(sh.misses, [])
        self.assertEqual(c["status"], "fail")
        self.assertIn("gcloud services enable cloudfunctions.googleapis.com --project demo-nonprod-1234", c["fix"])
