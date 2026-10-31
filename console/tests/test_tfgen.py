"""Workspace rendering (spec §4.3): shared workspaces, JSON-only Terraform, no secrets, guards.

`terraform validate` on every kind is opt-in (it downloads providers):
    CONSOLE_TF_VALIDATE=1 PYTHONPATH=client/src:. python -m unittest console.tests.test_tfgen
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from console import model, redact
from console.steps import tfgen

SUB = "11111111-2222-3333-4444-555555555555"
RG_ID = f"/subscriptions/{SUB}/resourceGroups/rg-apps"
KV = f"{RG_ID}/providers/Microsoft.KeyVault/vaults/kv-demo"
ENV = f"{RG_ID}/providers/Microsoft.App/managedEnvironments/cae-demo"
UAMI = f"{RG_ID}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-orders"


def gcp_cfg(kind="gcp_run_service", name="orders", project="demo-nonprod-1234", region="us-central1", subject="104857600000000000001"):
    cfg = model.default_config(kind, name=name)
    cfg["target"]["gcp"].update(project=project, region=region, name=name)
    cfg["pull_path"]["rotator_image"] = f"{region}-docker.pkg.dev/{project}/platform/jfrog-oidc-rotator:0.1.0"
    cfg["target"]["discovered"] = {"kind": kind, "mapping_subject": subject,
                                   "service_account": f"{name}@{project}.iam.gserviceaccount.com"}
    if kind == "gcp_run_function":
        cfg["target"]["discovered"]["build_service_account"] = f"builder@{project}.iam.gserviceaccount.com"
    return cfg


def azure_cfg(kind="azure_container_app", name="orders", principal="aaaaaaaa-0000-0000-0000-000000000001"):
    cfg = model.default_config(kind, name=name)
    cfg["target"]["azure"].update(subscription=SUB, resource_group="rg-apps", name=name, key_vault_id=KV, environment_id=ENV,
                                  identity_resource_id=UAMI)
    cfg["pull_path"]["rotator_image"] = "platformacr.azurecr.io/jfrog-oidc-rotator:0.1.0"
    provider = "Microsoft.App/containerApps" if kind in ("azure_container_app", "azure_functions_aca") else "Microsoft.Web/sites"
    cfg["target"]["discovered"] = {"kind": kind, "mapping_subject": principal, "principal_id": principal,
                                   "client_id": "cccccccc-0000-0000-0000-000000000001", "identity_type": "user_assigned",
                                   "identity_resource_id": UAMI, "location": "eastus",
                                   "resource_id": f"{RG_ID}/providers/{provider}/{name}"}
    return cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tfgen-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def render(self, cfg):
        return tfgen.render(cfg, self.root)

    def files(self, res):
        d = Path(res["dir"])
        return {n: json.loads((d / n).read_text()) for n in ("main.tf.json", "terraform.tfvars.json", "backend.tf.json")}


class AdoptIntegrationTests(Base):
    def test_adopted_integration_is_imported_not_created(self):
        cfg = gcp_cfg()
        cfg["jfrog"]["adopt_integration"] = True
        f = self.files(self.render(cfg))
        self.assertTrue(f["terraform.tfvars.json"]["adopt_jfrog_integration"])
        (imp,) = f["main.tf.json"]["import"]
        self.assertEqual(imp["to"], "module.jfrog_oidc.platform_oidc_configuration.gcp[0]")
        self.assertIn("var.adopt_jfrog_integration", imp["for_each"])
        self.assertEqual(imp["id"], "${each.value}")

    def test_default_creates(self):
        f = self.files(self.render(gcp_cfg()))
        self.assertFalse(f["terraform.tfvars.json"]["adopt_jfrog_integration"])

    def test_azure_import_address(self):
        cfg = azure_cfg()
        cfg["jfrog"]["adopt_integration"] = True
        (imp,) = self.files(self.render(cfg))["main.tf.json"]["import"]
        self.assertEqual(imp["to"], "module.jfrog_oidc.platform_oidc_configuration.azure[0]")


class KeyTests(Base):
    def test_workspace_keys(self):
        self.assertEqual(tfgen.workspace_key(gcp_cfg()), "gcp:demo-nonprod-1234:us-central1")
        self.assertEqual(tfgen.workspace_key(azure_cfg()), f"azure:{SUB}")

    def test_dir_is_under_workspaces_and_private(self):
        res = self.render(gcp_cfg())
        d = Path(res["dir"])
        self.assertEqual(d.parent, self.root / "workspaces")
        self.assertEqual(d.name, "gcp_demo-nonprod-1234_us-central1")
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        self.assertEqual((d / "terraform.tfvars.json").stat().st_mode & 0o777, 0o600)

    def test_hostile_key_parts_rejected(self):
        cfg = gcp_cfg()
        cfg["target"]["gcp"]["project"] = "../../etc"
        with self.assertRaises(tfgen.TfgenError):
            self.render(cfg)


class GcpRenderTests(Base):
    def test_service_workspace(self):
        res = self.render(gcp_cfg())
        f = self.files(res)
        main, tfvars = f["main.tf.json"], f["terraform.tfvars.json"]
        modules = Path(__file__).resolve().parents[2] / "terraform" / "modules"
        self.assertEqual(main["module"]["jfrog_oidc"]["source"], str(modules / "jfrog-oidc"))
        self.assertEqual(main["module"]["gcp_pull_path"]["source"], str(modules / "gcp-pull-path"))
        self.assertEqual(tfvars["workloads"]["orders-runtime"],
                         {"cloud": "gcp", "subject": "104857600000000000001", "username": None,
                          "groups": ["serverless-readers"], "expires_in": 3600,
                          "description": "gcp_run_service orders (runtime exchange)"})
        self.assertTrue(tfvars["pull_path"]["enabled"])
        self.assertEqual(tfvars["pull_mapping_name"], "gcp-demo-nonprod-1234-us-central1-pull")
        self.assertEqual(tfvars["remote_repositories"],
                         {"jfrog-docker": {"format": "DOCKER", "upstream_uri": "https://acme-docker-virtual.jfrog.io"}})
        self.assertEqual(res["targets"], ["orders"])

    def test_main_tf_json_holds_no_operator_values(self):
        # All data goes through tfvars: main.tf.json is the same for every GCP
        # workspace, so no operator value can be spliced into an expression.
        a = (Path(self.render(gcp_cfg())["dir"]) / "main.tf.json").read_text()
        cfg = gcp_cfg(project="other-proj-9", region="europe-west1", name="x")
        cfg["pull_path"]["name_prefix"] = "jfrog-eu"
        cfg["jfrog"]["integration_names"]["gcp"] = "gcp-cloudrun-eu"
        b = (Path(self.render(cfg)["dir"]) / "main.tf.json").read_text()
        self.assertEqual(a, b)
        for value in ("demo-nonprod-1234", "acme.jfrog.io", "104857600000000000001", "serverless-readers"):
            self.assertNotIn(value, a)

    def test_function_adds_build_sa_reader(self):
        f = self.files(self.render(gcp_cfg("gcp_run_function", name="webhook")))
        self.assertEqual(f["terraform.tfvars.json"]["reader_members"],
                         ["serviceAccount:builder@demo-nonprod-1234.iam.gserviceaccount.com"])

    def test_pull_path_off_everywhere(self):
        cfg = gcp_cfg()
        cfg["pull_path"]["enabled"] = False
        cfg["pull_path"]["rotator_image"] = ""
        f = self.files(self.render(cfg))
        self.assertFalse(f["terraform.tfvars.json"]["pull_path"]["enabled"])

    def test_requires_discover(self):
        cfg = gcp_cfg()
        del cfg["target"]["discovered"]
        with self.assertRaisesRegex(tfgen.TfgenError, "Discover"):
            self.render(cfg)

    def test_requires_rotator_image_with_pull_path(self):
        cfg = gcp_cfg()
        cfg["pull_path"]["rotator_image"] = ""
        with self.assertRaisesRegex(tfgen.TfgenError, "rotator_image"):
            self.render(cfg)

    def test_validation_errors_block(self):
        cfg = gcp_cfg()
        cfg["jfrog"]["url"] = "http://insecure"
        with self.assertRaisesRegex(tfgen.TfgenError, "https"):
            self.render(cfg)

    def test_no_secret_in_any_file(self):
        res = self.render(gcp_cfg())
        for p in Path(res["dir"]).iterdir():
            # main.tf.json is constant (see test_main_tf_json_holds_no_operator_values);
            # its output blocks use the key "value", which redact masks by design.
            if p.is_file() and p.name != "main.tf.json":
                text = p.read_text()
                self.assertEqual(redact.text(text), text, p.name)


class AksPilotLessonTests(Base):
    """Rules carried over from the AKS sandbox pilot (docs/aks-pilot-lessons.md)."""

    def test_docker_upstream_with_path_refused(self):
        cfg = gcp_cfg()
        cfg["pull_path"]["gcp_repositories"] = {"jfrog-docker": {"format": "DOCKER", "upstream_uri": "https://acme.jfrog.io/docker-virtual"}}
        with self.assertRaisesRegex(tfgen.TfgenError, "repo's own host"):
            self.render(cfg)

    def test_docker_upstream_on_platform_host_warns(self):
        cfg = gcp_cfg()
        cfg["pull_path"]["gcp_repositories"] = {"jfrog-docker": {"format": "DOCKER", "upstream_uri": "https://acme.jfrog.io"}}
        res = self.render(cfg)
        self.assertTrue(any("repository-path" in w for w in res["warnings"]))

    def test_jfrog_url_with_path_refused(self):
        cfg = gcp_cfg()
        cfg["jfrog"]["url"] = "https://acme.jfrog.io/artifactory"
        with self.assertRaisesRegex(tfgen.TfgenError, "origin only"):
            self.render(cfg)

    def test_one_mapping_per_identity(self):
        self.render(gcp_cfg(name="orders"))
        with self.assertRaisesRegex(tfgen.TfgenError, "same identity"):
            self.render(gcp_cfg("gcp_run_job", name="nightly"))   # same subject as orders


class SharedWorkspaceTests(Base):
    def test_two_targets_share_one_workspace(self):
        a = self.render(gcp_cfg(name="orders"))
        b = self.render(gcp_cfg("gcp_run_job", name="nightly", subject="104857600000000000002"))
        self.assertEqual(a["dir"], b["dir"])
        self.assertEqual(b["targets"], ["nightly", "orders"])
        w = self.files(b)["terraform.tfvars.json"]["workloads"]
        self.assertEqual(set(w), {"orders-runtime", "nightly-runtime"})

    def test_rerender_is_idempotent_and_updates(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg()
        cfg["runtime"]["ttl_seconds"] = 1800
        f = self.files(self.render(cfg))
        self.assertEqual(f["terraform.tfvars.json"]["workloads"]["orders-runtime"]["expires_in"], 1800)
        self.assertEqual(len(f["terraform.tfvars.json"]["workloads"]), 1)

    def test_repositories_merge(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg(name="api", subject="104857600000000000009")
        cfg["pull_path"]["gcp_repositories"] = {"jfrog-pypi": {"format": "PYTHON", "upstream_uri": "https://acme.jfrog.io/artifactory/api/pypi/pypi-virtual"}}
        f = self.files(self.render(cfg))
        self.assertEqual(set(f["terraform.tfvars.json"]["remote_repositories"]), {"jfrog-docker", "jfrog-pypi"})

    def test_conflicting_shared_setting_is_refused(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg(name="api", subject="104857600000000000009")
        cfg["pull_path"]["ttl_seconds"] = 7200
        with self.assertRaisesRegex(tfgen.TfgenError, "pull_path.ttl_seconds"):
            self.render(cfg)

    def test_same_repo_id_different_upstream_is_refused(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg(name="api", subject="104857600000000000009")
        cfg["pull_path"]["gcp_repositories"] = {"jfrog-docker": {"format": "DOCKER", "upstream_uri": "https://other.jfrog.io"}}
        with self.assertRaisesRegex(tfgen.TfgenError, "jfrog-docker"):
            self.render(cfg)

    def test_remove_target(self):
        self.render(gcp_cfg(name="orders"))
        self.render(gcp_cfg("gcp_run_job", name="nightly", subject="104857600000000000002"))
        res = tfgen.remove(gcp_cfg(name="orders"), self.root)
        self.assertEqual(res["targets"], ["nightly"])
        self.assertNotIn("orders-runtime", self.files(res)["terraform.tfvars.json"]["workloads"])

    def test_concurrent_renders_lose_nothing(self):
        cfgs = [gcp_cfg(name=f"svc-{i}", subject=str(104857600000000000100 + i)) for i in range(8)]
        errors = []

        def go(c):
            try:
                self.render(c)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=go, args=(c,)) for c in cfgs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        w = json.loads((self.root / "workspaces" / "gcp_demo-nonprod-1234_us-central1" / "terraform.tfvars.json").read_text())
        self.assertEqual(len(w["workloads"]), 8)


class GuardTests(Base):
    def test_gcp_prefix_collision_across_regions(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg(name="eu", region="europe-west1", subject="104857600000000000005")
        cfg["jfrog"]["integration_names"]["gcp"] = "gcp-cloudrun-eu"
        with self.assertRaisesRegex(tfgen.TfgenError, "name_prefix"):
            self.render(cfg)
        cfg["pull_path"]["name_prefix"] = "jfrog-eu"
        self.render(cfg)

    def test_integration_name_collision_across_workspaces(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg(name="b", project="other-proj-9", subject="104857600000000000005")
        with self.assertRaisesRegex(tfgen.TfgenError, "integration"):
            self.render(cfg)

    def test_mapping_name_collision_across_clouds(self):
        self.render(gcp_cfg(name="orders"))
        with self.assertRaisesRegex(tfgen.TfgenError, "orders-runtime"):
            self.render(azure_cfg(name="orders"))


class AzureRenderTests(Base):
    def test_container_app(self):
        f = self.files(self.render(azure_cfg()))
        t = f["terraform.tfvars.json"]
        self.assertEqual(t["azure_subscription_id"], SUB)
        self.assertEqual(t["location"], "eastus")
        self.assertEqual(t["resource_group_name"], "rg-apps")
        self.assertEqual(t["key_vault_uri"], "https://kv-demo.vault.azure.net/")
        self.assertEqual(t["container_app_environment_id"], ENV)
        self.assertTrue(t["pull_path"]["enabled"])
        self.assertEqual(t["audience_assignees"], {"orders": "aaaaaaaa-0000-0000-0000-000000000001"})
        self.assertEqual(t["secret_readers"], {"orders": "aaaaaaaa-0000-0000-0000-000000000001"})
        self.assertEqual(t["function_app_ids"], {})
        self.assertEqual(t["workloads"]["orders-runtime"]["cloud"], "azure")
        self.assertEqual(t["pull_mapping_name"], "azure-11111111-pull")

    def test_premium_refresh_and_acr(self):
        cfg = azure_cfg("azure_functions_premium", name="fn-orders")
        cfg["pull_path"]["rotator_acr_id"] = f"{RG_ID}/providers/Microsoft.ContainerRegistry/registries/platformacr"
        t = self.files(self.render(cfg))["terraform.tfvars.json"]
        self.assertEqual(t["function_app_ids"], {"fn-orders": f"{RG_ID}/providers/Microsoft.Web/sites/fn-orders"})
        self.assertEqual(t["rotator_registry"], {"server": "platformacr.azurecr.io", "acr_id": cfg["pull_path"]["rotator_acr_id"]})

    def test_flex_only_subscription_has_no_pull_path(self):
        cfg = azure_cfg("azure_functions_flex", name="fn-flex")
        cfg["target"]["azure"]["key_vault_id"] = ""
        cfg["target"]["azure"]["environment_id"] = ""
        cfg["pull_path"]["rotator_image"] = ""
        t = self.files(self.render(cfg))["terraform.tfvars.json"]
        self.assertFalse(t["pull_path"]["enabled"])
        self.assertEqual(t["secret_readers"], {})
        self.assertEqual(t["audience_assignees"], {"fn-flex": "aaaaaaaa-0000-0000-0000-000000000001"})

    def test_pull_path_needs_vault_and_environment(self):
        cfg = azure_cfg()
        cfg["target"]["azure"]["environment_id"] = ""
        with self.assertRaisesRegex(tfgen.TfgenError, "environment_id"):
            self.render(cfg)


class DedicatedIdentityTests(Base):
    """Existing apps without an identity of their own: the workspace creates one."""

    def test_gcp_default_account_gets_a_new_service_account(self):
        cfg = gcp_cfg(subject=None)
        cfg["target"]["discovered"]["dedicated_identity"] = "jfrog-orders"
        f = self.files(self.render(cfg))
        t, m = f["terraform.tfvars.json"], f["main.tf.json"]
        self.assertEqual(t["dedicated_service_accounts"], {"orders": "jfrog-orders"})
        self.assertEqual(t["workloads"]["orders-runtime"]["subject"], "")
        self.assertIn("dedicated", m["resource"]["google_service_account"])
        self.assertIn("local.runtime_workloads", m["module"]["jfrog_oidc"]["workloads"])
        self.assertIn("dedicated_service_accounts", m["output"])
        self.assertEqual(t["dedicated_service_account_roles"], {})

    def test_gcp_new_service_account_takes_over_roles(self):
        cfg = gcp_cfg(subject=None)
        cfg["target"]["discovered"].update(dedicated_identity="jfrog-orders",
                                           dedicated_service_account_roles=["roles/storage.objectViewer", "bad role"])
        f = self.files(self.render(cfg))
        t, m = f["terraform.tfvars.json"], f["main.tf.json"]
        self.assertEqual(t["dedicated_service_account_roles"], {"orders": ["roles/storage.objectViewer"]})
        grant = m["resource"]["google_project_iam_member"]["dedicated"]
        self.assertEqual(grant["for_each"], "${local.dedicated_role_grants}")

    def test_azure_no_identity_gets_a_user_assigned_one(self):
        cfg = azure_cfg(principal=None)
        cfg["target"]["discovered"].update(dedicated_identity="id-orders-jfrog", identity_resource_id=None, client_id=None)
        f = self.files(self.render(cfg))
        t, m = f["terraform.tfvars.json"], f["main.tf.json"]
        self.assertEqual(t["dedicated_identities"],
                         {"orders": {"name": "id-orders-jfrog", "resource_group_name": "rg-apps", "location": "eastus"}})
        self.assertEqual(t["audience_assignees"], {"orders": ""})
        self.assertEqual(t["secret_readers"], {"orders": ""})
        self.assertEqual(m["module"]["azure"]["audience_assignees"], "${local.audience_assignees}")
        self.assertEqual(m["module"]["azure"]["secret_readers"], "${local.secret_readers}")
        self.assertIn("dedicated_identities", m["output"])

    def test_dedicated_identity_must_belong_to_the_config(self):
        cfg = gcp_cfg(subject=None)
        cfg["target"]["discovered"]["dedicated_identity"] = "jfrog-someone-else"
        with self.assertRaisesRegex(tfgen.TfgenError, "does not belong"):
            self.render(cfg)

    def test_two_dedicated_targets_share_a_workspace(self):
        for name in ("orders", "api"):
            cfg = gcp_cfg(name=name, subject=None)
            cfg["target"]["discovered"]["dedicated_identity"] = f"jfrog-{name}"
            res = self.render(cfg)
        t = self.files(res)["terraform.tfvars.json"]
        self.assertEqual(t["dedicated_service_accounts"], {"api": "jfrog-api", "orders": "jfrog-orders"})


class BackendTests(Base):
    def test_backends(self):
        cfg = gcp_cfg()
        self.assertEqual(self.files(self.render(cfg))["backend.tf.json"],
                         {"terraform": {"backend": {"local": {"path": "terraform.tfstate"}}}})

    def test_remote_backend(self):
        cfg = gcp_cfg()
        cfg["state_backend"] = {"type": "gcs", "bucket": "tf-state-demo", "prefix": "jfrog-oidc"}
        self.assertEqual(self.files(self.render(cfg))["backend.tf.json"],
                         {"terraform": {"backend": {"gcs": {"bucket": "tf-state-demo", "prefix": "jfrog-oidc"}}}})

    def test_backend_change_on_shared_workspace_refused(self):
        self.render(gcp_cfg())
        cfg = gcp_cfg(name="api", subject="104857600000000000009")
        cfg["state_backend"] = {"type": "gcs", "bucket": "tf-state-demo", "prefix": "x"}
        with self.assertRaisesRegex(tfgen.TfgenError, "state_backend"):
            self.render(cfg)


@unittest.skipUnless(os.environ.get("CONSOLE_TF_VALIDATE") == "1" and shutil.which("terraform"),
                     "set CONSOLE_TF_VALIDATE=1 (downloads providers) to run terraform validate")
class TerraformValidateTests(Base):
    """M4 acceptance: `terraform validate` passes for generated workspaces of every kind."""

    def validate(self, cfg):
        d = Path(self.render(cfg)["dir"])
        (d / "backend.tf.json").unlink()      # validate needs no backend
        env = dict(os.environ, TF_IN_AUTOMATION="1")
        init = subprocess.run(["terraform", f"-chdir={d}", "init", "-backend=false", "-input=false", "-no-color"],
                              capture_output=True, text=True, timeout=600, env=env)
        self.assertEqual(init.returncode, 0, init.stderr[-2000:])
        val = subprocess.run(["terraform", f"-chdir={d}", "validate", "-json", "-no-color"],
                             capture_output=True, text=True, timeout=300, env=env)
        out = json.loads(val.stdout)
        self.assertTrue(out["valid"], json.dumps(out["diagnostics"], indent=1)[-3000:])
        shutil.rmtree(d)

    def test_every_kind(self):
        from console.tests.test_tfgen_aws_openshift import aws_cfg, openshift_cfg
        make = {"gcp": gcp_cfg, "azure": azure_cfg, "aws": aws_cfg, "openshift": openshift_cfg}
        for kind in model.KINDS:
            with self.subTest(kind=kind):
                self.validate(make[model.cloud_of(kind)](kind, name="t-" + kind.replace("_", "-")))

    def test_dedicated_identities(self):
        g = gcp_cfg(subject=None)
        g["target"]["discovered"]["dedicated_identity"] = "jfrog-orders"
        a = azure_cfg(principal=None)
        a["target"]["discovered"]["dedicated_identity"] = "id-orders-jfrog"
        for cfg in (g, a):
            self.validate(cfg)

    def test_pull_path_off(self):
        for cfg in (gcp_cfg(), azure_cfg("azure_functions_flex", name="fx")):
            cfg["pull_path"]["enabled"] = False
            self.validate(cfg)


if __name__ == "__main__":
    unittest.main()
