"""wire.py: desired vs current settings, diff hash approval, apply, verify (spec §4.4)."""

import json
import unittest

from console import model, redact
from console.shell import Shell
from console.steps import wire

P, R = "demo-nonprod-1234", "us-central1"
SUB = "11111111-2222-3333-4444-555555555555"
RG_ID = f"/subscriptions/{SUB}/resourceGroups/rg-apps"
UAMI = f"{RG_ID}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-orders"
KV_URL = "https://kv-demo.vault.azure.net/secrets/jfrog-registry-token"

GCP_OUT = {"jfrog_provider_name": "gcp-cloudrun", "jfrog_audience": "jfrog-gcp",
           "repository_urls": {"jfrog-docker": f"{R}-docker.pkg.dev/{P}/jfrog-docker",
                               "jfrog-pypi": f"https://{R}-python.pkg.dev/{P}/jfrog-pypi/simple/"}}
AZ_OUT = {"jfrog_provider_name": "azure-entra", "audience_uri": "api://dddddddd-0000-0000-0000-000000000001",
          "registry_username": "svc-azure-registry-pull", "registry_secret_versionless_url": KV_URL}
GCP_ENV = {"JFROG_OIDC_AUDIENCE": "jfrog-gcp", "JFROG_OIDC_PROVIDER": "gcp-cloudrun", "JFROG_OIDC_SOURCE": "gcp",
           "JFROG_URL": "https://acme.jfrog.io"}


def gcp_cfg(kind="gcp_run_service", name="orders"):
    cfg = model.default_config(kind, name=name)
    cfg["target"]["gcp"].update(project=P, region=R, name=name)
    cfg["target"]["discovered"] = {"kind": kind, "mapping_subject": "1048576", "run_service": name}
    return cfg


def azure_cfg(kind="azure_container_app", name="orders", identity="user_assigned"):
    cfg = model.default_config(kind, name=name)
    cfg["target"]["azure"].update(subscription=SUB, resource_group="rg-apps", name=name)
    prov = "Microsoft.App/containerApps" if kind in ("azure_container_app", "azure_functions_aca") else "Microsoft.Web/sites"
    cfg["target"]["discovered"] = {"kind": kind, "mapping_subject": "aaaa", "identity_type": identity,
                                   "identity_resource_id": UAMI if identity == "user_assigned" else None,
                                   "client_id": "cccccccc-0000-0000-0000-000000000001" if identity == "user_assigned" else None,
                                   "resource_id": f"{RG_ID}/providers/{prov}/{name}"}
    return cfg


def describe_svc(env, consume=False, name="orders"):
    fx = {"argv": ["gcloud", "run", "services", "describe", name, "--region", R, "--project", P, "--format=json"],
          "rc": 0, "stdout": json.dumps({"spec": {"template": {"spec": {"containers": [{"image": "x", "env": env}]}}}})}
    if consume:
        fx["consume"] = True
    return fx


def update_svc(name="orders", env=GCP_ENV):
    return {"argv": ["gcloud", "run", "services", "update", name, "--region", R, "--project", P,
                     "--update-env-vars=^@^" + "@".join(f"{k}={v}" for k, v in sorted(env.items())), "--quiet"], "rc": 0}


BEFORE = [{"name": "JFROG_URL", "value": "https://old.jfrog.io"}, {"name": "OTHER", "value": "keep"}]
AFTER = BEFORE[1:] + [{"name": k, "value": v} for k, v in GCP_ENV.items()]


class GcpTests(unittest.TestCase):
    def test_diff_only_touches_managed_keys(self):
        sh = Shell(mode="replay", fixtures=[describe_svc(BEFORE)])
        plan = wire.Wire(gcp_cfg(), sh, GCP_OUT).plan()
        by_key = {d["setting"]: d for d in plan["diff"]}
        self.assertEqual(set(by_key), set(GCP_ENV))
        self.assertEqual(by_key["JFROG_URL"]["action"], "change")
        self.assertEqual(by_key["JFROG_URL"]["current"], "https://old.jfrog.io")
        self.assertEqual(by_key["JFROG_OIDC_SOURCE"]["action"], "add")
        self.assertEqual(plan["commands"], [update_svc()["argv"]])
        self.assertEqual(len(plan["sha256"]), 64)
        self.assertFalse(plan["noop"])
        self.assertIn(f"{R}-docker.pkg.dev/{P}/jfrog-docker/<image>:<tag>", " ".join(plan["notes"]))
        self.assertNotIn("<jfrog-repo-key>", " ".join(plan["notes"]))

    def test_apply_needs_matching_approval(self):
        sh = Shell(mode="replay", fixtures=[describe_svc(BEFORE)])
        w = wire.Wire(gcp_cfg(), sh, GCP_OUT)
        plan = w.plan()
        for approval in (None, {}, {"diff_sha256": "0" * 64}):
            with self.assertRaises(wire.WireRefused):
                w.apply(plan, approval)
        self.assertFalse(any(" update " in m for m in sh.misses))

    def test_apply_refused_if_target_changed_since_approval(self):
        changed = [{"name": "JFROG_URL", "value": "https://someone-else.jfrog.io"}]
        sh = Shell(mode="replay", fixtures=[describe_svc(BEFORE, consume=True), describe_svc(changed)])
        w = wire.Wire(gcp_cfg(), sh, GCP_OUT)
        plan = w.plan()
        with self.assertRaises(wire.WireRefused) as cm:
            w.apply(plan, {"diff_sha256": plan["sha256"]})
        self.assertEqual(cm.exception.code, "target_changed")
        self.assertEqual(sh.misses, [])

    def test_apply_then_verify(self):
        sh = Shell(mode="replay", fixtures=[describe_svc(BEFORE, consume=True), describe_svc(BEFORE, consume=True),
                                            update_svc(), describe_svc(AFTER)])
        w = wire.Wire(gcp_cfg(), sh, GCP_OUT)
        plan = w.plan()
        res = w.apply(plan, {"diff_sha256": plan["sha256"]})
        self.assertEqual(sh.misses, [])
        self.assertTrue(res["applied"])
        self.assertTrue(res["verified"])

    def test_noop_when_already_wired(self):
        sh = Shell(mode="replay", fixtures=[describe_svc(AFTER)])
        plan = wire.Wire(gcp_cfg(), sh, GCP_OUT).plan()
        self.assertTrue(plan["noop"])
        self.assertEqual(plan["commands"], [])

    def test_job(self):
        job = json.dumps({"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"env": []}]}}}}}})
        sh = Shell(mode="replay", fixtures=[{"argv": ["gcloud", "run", "jobs", "describe", "nightly", "--region", R, "--project", P,
                                                      "--format=json"], "stdout": job}])
        plan = wire.Wire(gcp_cfg("gcp_run_job", "nightly"), sh, GCP_OUT).plan()
        self.assertEqual(plan["commands"][0][:4], ["gcloud", "run", "jobs", "update"])

    def test_function_wires_underlying_service_and_notes_index_url(self):
        sh = Shell(mode="replay", fixtures=[describe_svc([], name="webhook")])
        plan = wire.Wire(gcp_cfg("gcp_run_function", "webhook"), sh, GCP_OUT).plan()
        self.assertEqual(plan["commands"], [update_svc("webhook")["argv"]])
        notes = " ".join(plan["notes"])
        self.assertIn(f"--index-url https://{R}-python.pkg.dev/{P}/jfrog-pypi/simple/", notes)
        self.assertIn("gcloud functions deploy", notes)

    def test_missing_outputs(self):
        with self.assertRaisesRegex(wire.WireError, "Apply"):
            wire.Wire(gcp_cfg(), Shell(mode="replay"), {}).plan()

    def test_iac_snippet(self):
        sh = Shell(mode="replay", fixtures=[describe_svc(BEFORE)])
        self.assertEqual(wire.Wire(gcp_cfg(), sh, GCP_OUT).plan()["iac"]["env"], GCP_ENV)


def aca_show(env):
    return json.dumps({"properties": {"template": {"containers": [{"name": "app", "image": "acme.jfrog.io/docker/orders:1", "env": env}]},
                                      "configuration": {"secrets": [], "registries": []}}})


ACA_SHOW_ARGV = ["az", "containerapp", "show", "-g", "rg-apps", "-n", "orders", "--subscription", SUB, "-o", "json"]


class DedicatedIdentityTests(unittest.TestCase):
    """Existing apps without their own identity: Wire attaches the one the workspace created."""

    def test_gcp_switches_service_account_in_the_same_update(self):
        cfg = gcp_cfg()
        cfg["target"]["discovered"].update(dedicated_identity="jfrog-orders", mapping_subject=None,
                                           service_account="1-compute@developer.gserviceaccount.com",
                                           replaced_service_account_roles=["roles/editor"])
        new = f"jfrog-orders@{P}.iam.gserviceaccount.com"
        sh = Shell(mode="replay", fixtures=[describe_svc([])])
        plan = wire.Wire(cfg, sh, {**GCP_OUT, "dedicated_service_accounts": {"orders": new}}).plan()
        self.assertEqual(plan["diff"][0], {"scope": "identity", "setting": "service account", "action": "change",
                                           "current": "1-compute@developer.gserviceaccount.com", "desired": new})
        self.assertEqual(len(plan["commands"]), 1)
        self.assertEqual(plan["commands"][0][-2], f"--service-account={new}")
        self.assertTrue(any("roles/editor" in n for n in plan["notes"]))

    def test_gcp_needs_the_output(self):
        cfg = gcp_cfg()
        cfg["target"]["discovered"]["dedicated_identity"] = "jfrog-orders"
        with self.assertRaisesRegex(wire.WireError, "dedicated_service_accounts"):
            wire.Wire(cfg, Shell(mode="replay", fixtures=[]), GCP_OUT)

    def test_container_app_assigns_identity_first_and_uses_it(self):
        cfg = azure_cfg(identity="user_assigned")
        cfg["target"]["discovered"].update(dedicated_identity="id-orders-jfrog", identity_resource_id=None, client_id=None)
        made = {"id": f"{RG_ID}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-orders-jfrog",
                "client_id": "cccccccc-0000-0000-0000-00000000dddd", "principal_id": "pp"}
        sh = Shell(mode="replay", fixtures=[{"argv": ACA_SHOW_ARGV, "stdout": aca_show([])}])
        plan = wire.Wire(cfg, sh, {**AZ_OUT, "dedicated_identities": {"orders": made}}).plan()
        self.assertEqual(plan["commands"][0], ["az", "containerapp", "identity", "assign", "-g", "rg-apps", "-n", "orders",
                                               "--subscription", SUB, "--user-assigned", made["id"], "-o", "none"])
        env = {d["setting"]: d["desired"] for d in plan["diff"] if d["scope"] == "env"}
        self.assertEqual(env["AZURE_CLIENT_ID"], made["client_id"])
        self.assertIn(f"identityref:{made['id']}", " ".join(" ".join(c) for c in plan["commands"]))

    def test_premium_assigns_identity_before_key_vault_reference(self):
        cfg = azure_cfg("azure_functions_premium", "fn")
        cfg["target"]["discovered"].update(dedicated_identity="id-fn-jfrog", identity_resource_id=None, client_id=None)
        made = {"id": f"{RG_ID}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-fn-jfrog", "client_id": "c"}
        fx = AzureTests._premium(None, [])
        sh = Shell(mode="replay", fixtures=fx)
        plan = wire.Wire(cfg, sh, {**AZ_OUT, "dedicated_identities": {"fn": made}}).plan()
        self.assertEqual(plan["commands"][0][:4], ["az", "functionapp", "identity", "assign"])
        self.assertEqual(plan["commands"][1][-3], f"keyVaultReferenceIdentity={made['id']}")


class AzureTests(unittest.TestCase):
    def test_container_app(self):
        sh = Shell(mode="replay", fixtures=[{"argv": ACA_SHOW_ARGV, "stdout": aca_show([{"name": "OTHER", "value": "x"}])}])
        plan = wire.Wire(azure_cfg(), sh, AZ_OUT).plan()
        keys = {(d["scope"], d["setting"]): d for d in plan["diff"]}
        self.assertEqual(keys[("env", "AZURE_CLIENT_ID")]["desired"], "cccccccc-0000-0000-0000-000000000001")
        self.assertEqual(keys[("env", "JFROG_OIDC_AUDIENCE")]["desired"], AZ_OUT["audience_uri"])
        self.assertEqual(keys[("secret", "jfrog-registry-token")]["desired"], f"Key Vault {KV_URL} via {UAMI}")
        # The registry is the Docker repo's own host, never the platform host (AKS pilot, Phase 3b).
        self.assertNotIn(("registry", "acme.jfrog.io"), keys)
        self.assertEqual(keys[("registry", "acme-docker-virtual.jfrog.io")]["desired"],
                         "svc-azure-registry-pull / secret jfrog-registry-token")
        cmds = plan["commands"]
        self.assertEqual(cmds[0][:3], ["az", "containerapp", "update"])
        self.assertIn("--container-name", cmds[0])
        self.assertIn(f"jfrog-registry-token=keyvaultref:{KV_URL},identityref:{UAMI}", cmds[1])
        self.assertEqual(cmds[2], ["az", "containerapp", "registry", "set", "-g", "rg-apps", "-n", "orders", "--subscription", SUB,
                                   "--server", "acme-docker-virtual.jfrog.io", "--username", "svc-azure-registry-pull",
                                   "--password-secret-name", "jfrog-registry-token", "-o", "none"])
        self.assertNotIn(KV_URL, redact.text(" ".join(cmds[1])))

    def test_container_app_docker_host_override(self):
        cfg = azure_cfg()
        cfg["jfrog"]["docker_host"] = "example-aks-sandbox-test-docker-local.jfrog.io"
        sh = Shell(mode="replay", fixtures=[{"argv": ACA_SHOW_ARGV, "stdout": aca_show([])}])
        plan = wire.Wire(cfg, sh, AZ_OUT).plan()
        self.assertIn("example-aks-sandbox-test-docker-local.jfrog.io", plan["commands"][-1])
        self.assertTrue(any("not an image from example-aks-sandbox-test-docker-local.jfrog.io" in n for n in plan["notes"]))

    def test_container_app_without_docker_host_refuses(self):
        cfg = azure_cfg()
        cfg["jfrog"]["docker_repo"] = ""
        sh = Shell(mode="replay", fixtures=[{"argv": ACA_SHOW_ARGV, "stdout": aca_show([])}])
        with self.assertRaisesRegex(wire.WireError, "docker_repo"):
            wire.Wire(cfg, sh, AZ_OUT).plan()

    def test_system_assigned_has_no_client_id(self):
        sh = Shell(mode="replay", fixtures=[{"argv": ACA_SHOW_ARGV, "stdout": aca_show([])}])
        plan = wire.Wire(azure_cfg(identity="system_assigned"), sh, AZ_OUT).plan()
        self.assertNotIn("AZURE_CLIENT_ID", [d["setting"] for d in plan["diff"]])
        self.assertIn("identityref:system", " ".join(plan["commands"][1]))

    def _premium(self, settings, kv_identity="SystemAssigned"):
        return [
            {"argv": ["az", "functionapp", "config", "appsettings", "list", "-g", "rg-apps", "-n", "fn", "--subscription", SUB, "-o", "json"],
             "stdout": json.dumps(settings)},
            {"argv": ["az", "functionapp", "show", "-g", "rg-apps", "-n", "fn", "--subscription", SUB, "-o", "json"],
             "stdout": json.dumps({"keyVaultReferenceIdentity": kv_identity,
                                   "siteConfig": {"linuxFxVersion": "DOCKER|acme.jfrog.io/docker/fn:1"}})},
        ]

    def test_premium_never_exposes_other_settings(self):
        settings = [{"name": "AzureWebJobsStorage", "value": "DefaultEndpointsProtocol=https;AccountKey=c2VjcmV0c2VjcmV0"},
                    {"name": "DOCKER_REGISTRY_SERVER_PASSWORD", "value": "a-real-old-password"}]
        sh = Shell(mode="replay", fixtures=self._premium(settings))
        plan = wire.Wire(azure_cfg("azure_functions_premium", "fn"), sh, AZ_OUT).plan()
        text = json.dumps(plan)
        self.assertNotIn("AccountKey", text)
        self.assertNotIn("a-real-old-password", text)
        pw = [d for d in plan["diff"] if d["setting"] == "DOCKER_REGISTRY_SERVER_PASSWORD"][0]
        self.assertEqual(pw["current"], "[set, not shown]")
        self.assertEqual(pw["desired"], f"@Microsoft.KeyVault(SecretUri={KV_URL})")
        ident = [d for d in plan["diff"] if d["setting"] == "keyVaultReferenceIdentity"][0]
        self.assertEqual((ident["current"], ident["desired"]), ("SystemAssigned", UAMI))
        settings_cmd = [c for c in plan["commands"] if c[3:5] == ["appsettings", "set"]][0]
        self.assertEqual(settings_cmd[-2:], ["-o", "none"])
        self.assertIn(["az", "functionapp", "update", "-g", "rg-apps", "-n", "fn", "--subscription", SUB,
                       "--set", f"keyVaultReferenceIdentity={UAMI}", "-o", "none"], plan["commands"])

    def test_premium_settings_read_is_sensitive(self):
        settings = [{"name": "AzureWebJobsStorage", "value": "AccountKey=c2VjcmV0c2VjcmV0"}]
        sh = Shell(mode="record", fixtures=[], which=lambda n: None)
        sh._exec = lambda argv, t, e: __import__("console.shell", fromlist=["Result"]).Result(
            argv, 0, json.dumps(settings) if "appsettings" in argv else json.dumps({"keyVaultReferenceIdentity": "SystemAssigned"}))
        wire.Wire(azure_cfg("azure_functions_premium", "fn"), sh, AZ_OUT).plan()
        self.assertNotIn("AccountKey", json.dumps(sh.recorded))

    def test_flex_env_only(self):
        sh = Shell(mode="replay", fixtures=self._premium([]))
        cfg = azure_cfg("azure_functions_flex", "fn")
        cfg["pull_path"]["enabled"] = False
        plan = wire.Wire(cfg, sh, dict(AZ_OUT, registry_username=None, registry_secret_versionless_url=None)).plan()
        self.assertEqual({d["scope"] for d in plan["diff"]}, {"env"})
        self.assertEqual(len(plan["commands"]), 1)


if __name__ == "__main__":
    unittest.main()
