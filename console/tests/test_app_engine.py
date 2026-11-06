"""App Engine standard (gcp_app_engine): Discover, Wire, Verify, logs, live status, provenance, preflight and
the wiring into the stack list, pipelines and UI. Recorded answers only; no cloud access."""

import json
import re
import unittest
from pathlib import Path

from console import gae, live, model
from console.shell import Shell
from console.steps import discover, preflight, provenance, verify, wire
from console.steps import workload_logs as wl

ROOT = Path(__file__).resolve().parents[2]
P, R = "demo-nonprod-1234", "us-central1"
SA = f"web-runtime@{P}.iam.gserviceaccount.com"
VERSION = {"id": "20261105t120000", "serviceAccount": SA, "runtime": "python312",
           "versionUrl": f"https://20261105t120000-dot-web-dot-{P}.uc.r.appspot.com",
           "envVariables": {"JFROG_URL": "https://acme.jfrog.io", "OTHER": "keep"}}
SERVICE = {"id": "web", "split": {"allocations": {"20261101t000000": 0.0, "20261105t120000": 1.0}}}


def fx(argv, out, rc=0, stderr=""):
    return {"argv": argv, "rc": rc, "stdout": out if isinstance(out, str) else json.dumps(out), "stderr": stderr}


def cfg(name="web", discovered=None):
    c = model.default_config("gcp_app_engine", name=name)
    c["target"]["gcp"].update(project=P, region=R, name=name)
    if discovered is not None:
        c["target"]["discovered"] = discovered
    return c


def serving(version=VERSION, name="web"):
    return [fx(gae.service_argv(P, name), SERVICE), fx(gae.version_argv(P, name, "20261105t120000"), version)]


class HelperTests(unittest.TestCase):
    def test_kind_is_gcp(self):
        self.assertEqual(model.cloud_of("gcp_app_engine"), "gcp")
        self.assertEqual(model.KINDS["gcp_app_engine"]["label"], "App Engine service")

    def test_serving_version_is_the_largest_share(self):
        self.assertEqual(gae.serving_version(SERVICE), "20261105t120000")
        self.assertIsNone(gae.serving_version({"split": {"allocations": {}}}))
        self.assertIsNone(gae.serving_version({}))

    def test_service_url(self):
        self.assertEqual(gae.service_url("p.uc.r.appspot.com\n", "web"), "https://web-dot-p.uc.r.appspot.com")
        self.assertEqual(gae.service_url("p.uc.r.appspot.com", "default"), "https://p.uc.r.appspot.com")


class DiscoverTests(unittest.TestCase):
    def test_service_account_of_the_serving_version(self):
        sh = Shell(mode="replay", fixtures=[*serving(), fx(["gcloud", "iam", "service-accounts", "describe", SA,
                                                            "--project", P, "--format=json"],
                                                           {"email": SA, "uniqueId": "104857600000000000009"})])
        res = discover.run(cfg(), sh)
        self.assertEqual((sh.misses, res["errors"]), ([], []))
        self.assertEqual(res["kind"], "gcp_app_engine")
        self.assertEqual(res["facts"]["mapping_subject"], "104857600000000000009")
        self.assertEqual(res["facts"]["service_account"], SA)
        self.assertEqual(res["facts"]["version"], "20261105t120000")
        self.assertEqual(res["facts"]["runtime"], "python312")

    def test_default_app_engine_account_gets_a_dedicated_identity(self):
        version = {**VERSION, "serviceAccount": None}
        sh = Shell(mode="replay", fixtures=[*serving(version),
                                            fx(["gcloud", "projects", "get-iam-policy", P, "--format=json"], {"bindings": []})])
        res = discover.run(cfg(), sh)
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["facts"]["service_account"], f"{P}@appspot.gserviceaccount.com")
        self.assertTrue(res["facts"]["default_service_account"])
        self.assertIsNone(res["facts"]["mapping_subject"])
        self.assertEqual(res["facts"]["dedicated_identity"], model.dedicated_identity_name(cfg()))

    def test_missing_service_is_not_found(self):
        sh = Shell(mode="replay", fixtures=[fx(gae.service_argv(P, "web"), "", rc=1,
                                               stderr="ERROR: (gcloud.app.services.describe) NOT_FOUND: Service not found")])
        res = discover.run(cfg(), sh)
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["errors"][0]["code"], "not_found")

    def test_nothing_serving_is_an_error(self):
        sh = Shell(mode="replay", fixtures=[fx(gae.service_argv(P, "web"), {"id": "web", "split": {"allocations": {}}})])
        res = discover.run(cfg(), sh)
        self.assertEqual(res["errors"][0]["code"], "no_serving_version")


class WireTests(unittest.TestCase):
    OUT = {"jfrog_provider_name": "gcp-cloudrun", "jfrog_audience": "jfrog-gcp"}

    def test_diff_against_the_serving_version_and_no_command(self):
        sh = Shell(mode="replay", fixtures=serving())
        plan = wire.Wire(cfg(discovered={"kind": "gcp_app_engine", "mapping_subject": "1"}), sh, self.OUT).plan()
        self.assertEqual(sh.misses, [])
        by = {d["setting"]: d["action"] for d in plan["diff"]}
        self.assertEqual(by["JFROG_URL"], "same")
        self.assertEqual(by["JFROG_OIDC_PROVIDER"], "add")
        # App Engine changes variables only by a deploy: build-deploy writes them into app.yaml.
        self.assertEqual(plan["commands"], [])
        self.assertTrue(any("build-deploy" in n for n in plan["notes"]))
        self.assertEqual(plan["target"], f"gcp:{P}:{R}:appengine/web")


class VerifyTests(unittest.TestCase):
    def test_health_endpoint_on_the_service_url(self):
        ok = {"ok": True, "claims": {"sub": "1"}, "check": {"username": "u", "expires_in": 3599}, "read": None}
        sh = Shell(mode="replay", fixtures=[fx(gae.app_argv(P), f"{P}.uc.r.appspot.com\n"),
                                            {"http": f"GET https://web-dot-{P}.uc.r.appspot.com/healthz/jfrog",
                                             "status": 200, "body": json.dumps(ok)}])
        res = verify.run(cfg(), sh)
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))

    def test_no_app_is_a_clear_fail(self):
        sh = Shell(mode="replay", fixtures=[fx(gae.app_argv(P), "", rc=1, stderr="does not contain an App Engine application")])
        res = verify.run(cfg(), sh)
        self.assertEqual(res["status"], "fail")
        self.assertIn("App Engine", res["reason"])


class LogsLiveProvenanceTests(unittest.TestCase):
    def test_logs_filter_on_the_module(self):
        argv = wl.argv(cfg(), since="2026-11-05T00:00:00Z")
        self.assertIn('resource.type="gae_app" AND resource.labels.module_id="web"', argv[3])

    def test_live_status_reads_the_serving_version(self):
        wired = {**VERSION, "envVariables": {"JFROG_OIDC_PROVIDER": "gcp-cloudrun"}}
        sh = Shell(mode="replay", fixtures=[*serving(wired), fx(gae.service_argv(P, "gone"), "", rc=1,
                                                                stderr="NOT_FOUND: Service not found")])
        got = live.status("gcp", [cfg("web"), cfg("gone")], sh)
        self.assertEqual({k: v["state"] for k, v in got.items()}, {"web": "wired", "gone": "missing"})

    def test_provenance_is_a_source_package_from_its_own_pipeline(self):
        self.assertEqual(provenance.pipeline_yaml("gcp_app_engine"),
                         "pipelines/app-services/gcp/gcp-app-engine/build-deploy.yml")
        self.assertIn("gcp_app_engine", provenance.CODE_KINDS)
        self.assertEqual(provenance.running_image(cfg(), Shell(mode="replay", fixtures=[])), "")

    def test_prove_reads_the_serving_version_variables(self):
        from console.steps import prove
        sh = Shell(mode="replay", fixtures=serving())
        got = prove.read_settings(cfg(), sh)
        self.assertEqual((sh.misses, got["names"], got["registry_passwords"]), ([], ["JFROG_URL", "OTHER"], 0))

    def test_preflight_permissions_and_api(self):
        self.assertIn("appengine.versions.get", preflight.GCP_PERMISSIONS_BY_KIND["gcp_app_engine"])
        self.assertEqual(preflight.GCP_APIS_BY_KIND["gcp_app_engine"], ["appengine.googleapis.com"])


class InventoryTests(unittest.TestCase):
    def test_app_engine_services_are_listed_and_a_missing_app_is_quiet(self):
        from console import inventory
        from console.tests import test_inventory as ti

        class Fake(ti.Fake):
            def __init__(self, services):
                super().__init__()
                self.services = services

            def _replay_cmd(self, argv):
                if argv[:4] == ["gcloud", "app", "services", "list"]:
                    self.calls.append(list(argv))
                    if self.services is None:
                        return ti.Result(argv, 1, "", "ERROR: does not contain an App Engine application", 0.0)
                    return ti.Result(argv, 0, json.dumps(self.services), "", 0.0)
                return super()._replay_cmd(argv)

        errors = []
        _, found = inventory.gcp(Fake([{"id": "default"}, {"id": "web"}]), errors)
        self.assertEqual([w["name"] for w in found if w["kind"] == "gcp_app_engine"], ["default", "web"])
        errors = []
        _, found = inventory.gcp(Fake(None), errors)
        self.assertEqual(([w for w in found if w["kind"] == "gcp_app_engine"], errors), ([], []))


class WiringTests(unittest.TestCase):
    def test_stack_pipelines_and_app(self):
        for f in ("terraform/stacks/app-services/gcp/gcp-app-engine/main.tf",
                  "pipelines/app-services/gcp/gcp-app-engine/stack-setup.yml",
                  "pipelines/app-services/gcp/gcp-app-engine/build-deploy.yml",
                  "apps/web-app/app-engine/app.yaml"):
            self.assertTrue((ROOT / f).is_file(), f)
        self.assertIn("kind: app-engine", (ROOT / "pipelines/app-services/gcp/gcp-app-engine/build-deploy.yml").read_text())
        self.assertIn("app-engine)", (ROOT / "pipelines/scripts/gcp-build-deploy.sh").read_text())
        self.assertIn("'app-engine'", (ROOT / "pipelines/templates/gcp-build-deploy.yml").read_text())
        envs = (ROOT / "terraform/stacks/serverless/gcp/gcp-pipeline-identity/environments.tf").read_text()
        self.assertIn('"gcp-app-engine"', envs)

    def test_app_yaml_holds_no_credential(self):
        text = (ROOT / "apps/web-app/app-engine/app.yaml").read_text()
        self.assertNotRegex(text, re.compile(r"TOKEN|PASSWORD|SECRET", re.I))

    def test_ui_and_portal_know_the_kind(self):
        from portal import chain
        self.assertEqual(chain.KIND_OF["gcp-app-engine"], "gcp_app_engine")
        for f in ("console/static/shell.js", "console/static/list.js", "portal/static/data.js", "setup_console/static/app.js"):
            self.assertIn("app", (ROOT / f).read_text())
            self.assertRegex((ROOT / f).read_text(), r"gcp_app_engine|gcp-app-engine", f)


if __name__ == "__main__":
    unittest.main()
