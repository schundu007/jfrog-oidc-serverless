"""GKE (GCP.md Option B, Workload Identity): the cluster read, the values, the JFrog requests, the
Google service account step that adopts what exists, and a full set up against a fake gcloud."""

import json
import tempfile
import unittest
from pathlib import Path

from kcp_console import direct, model, platforms
from kcp_console.shell import Result
from kcp_console.tests.test_direct import FakeShell

ISSUER = "https://container.googleapis.com/v1/projects/proj-a/locations/us-central1/clusters/gke-a"
GSA = "jfrog-kcp@proj-a.iam.gserviceaccount.com"
MEMBER = "serviceAccount:proj-a.svc.id.goog[jfrog/jfrog-provider-sa]"


def gke_cfg(**cluster):
    c = model.default_config("gke-a")
    c["platform"] = "gke"
    c["cluster"].update({"name": "gke-a", "project_id": "proj-a", "location": "us-central1", "resource_group": "",
                         "subscription_id": "", "subscription_name": "", "oidc_issuer": "", "google_service_account": ""})
    c["cluster"].update(cluster)
    c["values"]["provider_name"] = ""
    return c


class GkeShell(FakeShell):
    """gcloud for one GKE cluster; the Google service account and its bindings may or may not exist."""

    def __init__(self, wi=True, gsa=False, bindings=(), **kw):
        super().__init__(**kw)
        self.wi, self.gsa, self.bindings = wi, gsa, set(bindings)

    def _ok(self, argv, out="", rc=0, err="", extra_env=None):
        self.calls.append(list(argv))
        self.env_seen.append(extra_env or {})
        return Result(list(argv), rc, out, err, 0.0)

    def run(self, argv, timeout=60.0, sensitive=False, extra_env=None):
        head = argv[:4]
        if head == ["gcloud", "container", "clusters", "describe"]:
            d = {"currentMasterVersion": "1.33.5-gke.1000", "location": "us-central1", "currentNodeCount": 2}
            if self.wi:
                d["workloadIdentityConfig"] = {"workloadPool": "proj-a.svc.id.goog"}
            return self._ok(argv, json.dumps(d), extra_env=extra_env)
        if head == ["gcloud", "container", "clusters", "update"]:
            self.wi = True
            return self._ok(argv, extra_env=extra_env)
        if head == ["gcloud", "container", "clusters", "get-credentials"]:
            Path(extra_env["KUBECONFIG"]).write_text("apiVersion: v1\nkind: Config\n")
            return self._ok(argv, extra_env=extra_env)
        if head == ["gcloud", "iam", "service-accounts", "describe"]:
            return self._ok(argv, json.dumps({"email": GSA}) if self.gsa else "", 0 if self.gsa else 1,
                            "" if self.gsa else "NOT_FOUND", extra_env)
        if head == ["gcloud", "iam", "service-accounts", "create"]:
            self.gsa = True
            return self._ok(argv, extra_env=extra_env)
        if head == ["gcloud", "iam", "service-accounts", "get-iam-policy"]:
            by = {}
            for role, member in self.bindings:
                by.setdefault(role, []).append(member)
            return self._ok(argv, json.dumps({"bindings": [{"role": r, "members": m} for r, m in by.items()]}), extra_env=extra_env)
        if head == ["gcloud", "iam", "service-accounts", "add-iam-policy-binding"]:
            self.bindings.add((argv[argv.index("--role") + 1], argv[argv.index("--member") + 1]))
            return self._ok(argv, extra_env=extra_env)
        if argv[:1] == ["gke-gcloud-auth-plugin"]:
            return self._ok(argv, "v1", extra_env=extra_env)
        return super().run(argv, timeout, sensitive, extra_env)


class PlatformTest(unittest.TestCase):
    def test_issuer_and_identity_names(self):
        c = model.merged(gke_cfg())
        self.assertEqual(platforms.issuer(c), ISSUER)
        self.assertEqual(platforms.gsa_email(c), GSA)
        self.assertEqual(platforms.wi_member(c), MEMBER)
        self.assertEqual(platforms.gsa_email(model.merged(gke_cfg(google_service_account="puller@x.iam.gserviceaccount.com"))),
                         "puller@x.iam.gserviceaccount.com")

    def test_cluster_read(self):
        c = model.merged(gke_cfg())
        self.assertEqual(platforms.show(c)[:4], ["gcloud", "container", "clusters", "describe"])
        f = platforms.parse(c, {"currentMasterVersion": "1.33.5-gke.1000", "location": "us-central1",
                                "workloadIdentityConfig": {"workloadPool": "proj-a.svc.id.goog"}})
        self.assertEqual((f["issuer"], f["oidc"], f["wi"], f["version"]), (ISSUER, True, True, "1.33.5-gke.1000"))
        self.assertFalse(platforms.parse(c, {})["wi"])
        self.assertIn("--workload-pool=proj-a.svc.id.goog", platforms.enable(c))

    def test_aks_unchanged(self):
        c = model.merged(model.default_config())
        self.assertEqual(platforms.show(c)[:3], ["az", "aks", "show"])
        self.assertIn("--enable-oidc-issuer", platforms.enable(c))


class ListTest(unittest.TestCase):
    """Existing clusters in every project the operator's gcloud can see, so current estates can be onboarded."""

    def test_lists_standard_and_autopilot_clusters_across_projects(self):
        class Sh:
            def run(self, argv, timeout=60.0, sensitive=False, extra_env=None):
                if argv[:3] == ["gcloud", "projects", "list"]:
                    return Result(argv, 0, json.dumps([{"projectId": "proj-a", "name": "Proj A"}, {"projectId": "proj-b", "name": "B"}]), "", 0.0)
                if argv[:4] == ["gcloud", "container", "clusters", "list"] and "proj-a" in argv:
                    return Result(argv, 0, json.dumps([
                        {"name": "gke-a", "location": "us-central1", "currentMasterVersion": "1.33.5", "status": "RUNNING",
                         "currentNodeCount": 2, "workloadIdentityConfig": {"workloadPool": "proj-a.svc.id.goog"}},
                        {"name": "auto", "location": "us-east1", "autopilot": {"enabled": True}}]), "", 0.0)
                return Result(argv, 1, "", "Kubernetes Engine API has not been used in project proj-b", 0.0)
        out = platforms.gke_list(Sh(), {"gke-a"})
        by = {c["name"]: c for c in out["clusters"]}
        self.assertEqual((by["gke-a"]["project_id"], by["gke-a"]["location"], by["gke-a"]["workload_identity"], by["gke-a"]["configured"]),
                         ("proj-a", "us-central1", True, True))
        self.assertEqual(by["gke-a"]["project_name"], "Proj A")
        self.assertTrue(by["auto"]["autopilot"])
        self.assertEqual(out["projects"], 2)
        self.assertNotIn("error", out)

    def test_no_projects_is_an_error(self):
        class Sh:
            def run(self, argv, timeout=60.0, sensitive=False, extra_env=None):
                return Result(argv, 1, "", "ERROR: (gcloud.projects.list) You do not currently have an active account selected.", 0.0)
        self.assertIn("active account", platforms.gke_list(Sh(), set())["error"])


class ModelTest(unittest.TestCase):
    def test_values_use_the_gcp_block(self):
        c = direct._resolved(gke_cfg())
        v = model.render_values(c)
        self.assertIn("    gcp:\n      enabled: true", v)
        self.assertIn(f'google_service_account_email: "{GSA}"', v)
        self.assertIn('jfrog_oidc_audience: "artifactory"', v)
        self.assertIn('jfrog_oidc_provider_name: "gke-gke-a-oidc"', v)
        self.assertNotIn("azure", v)

    def test_jfrog_requests_trust_the_gke_issuer(self):
        c = model.merged(gke_cfg(oidc_issuer=ISSUER))
        p = model.provider_request(c)
        self.assertEqual((p["provider_type"], p["issuer_url"], p["audience"]), ("Generic OpenID Connect", ISSUER, "artifactory"))
        m = model.mapping_request(c)
        self.assertEqual(m["claims"], {"iss": ISSUER, "sub": "system:serviceaccount:jfrog:jfrog-provider-sa", "aud": "artifactory"})

    def test_validation(self):
        fields = {i["field"] for i in model.errors(model.validate(gke_cfg(oidc_issuer=ISSUER)))}
        self.assertNotIn("platform", fields)
        self.assertIn("cluster.project_id is empty", direct.problems(gke_cfg(project_id="")))
        self.assertIn("cluster.location is empty", direct.problems(gke_cfg(location="")))
        self.assertNotIn("cluster.resource_group is empty", direct.problems(gke_cfg()))

    def test_service_account_names_the_google_account(self):
        sa = direct.sa_manifest(direct._resolved(gke_cfg()))["items"][1]
        self.assertEqual(sa["metadata"]["annotations"], {"JFrogExchange": "true", "iam.gke.io/gcp-service-account": GSA})


class PlanTest(unittest.TestCase):
    def test_setup_has_the_google_account_step(self):
        steps = direct.plan(gke_cfg(), Path(tempfile.mkdtemp()), "setup", dry_run=True)
        self.assertEqual([s["id"] for s in steps][:4], ["cluster", "oidc", "kubeconfig", "gsa"])
        by = {s["id"]: s for s in steps}
        self.assertIn("gcloud container clusters describe gke-a", by["cluster"]["command"])
        self.assertIn("--workload-pool=proj-a.svc.id.goog", by["oidc"]["command"])
        self.assertIn(ISSUER, by["provider"]["command"])
        self.assertNotIn("az ", " ".join(s["command"] for s in steps))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def run_setup(self, sh, dry):
        rn = direct.Runner(gke_cfg(), sh, self.root, {"JFROG_ACCESS_TOKEN": "t"}, lambda line: None, dry_run=dry, sleep=lambda s: None)
        return rn.execute(direct.plan(gke_cfg(), self.root, "setup", dry_run=dry))

    def test_real_setup_creates_what_is_missing(self):
        sh = GkeShell(wi=False)
        res = self.run_setup(sh, dry=False)
        self.assertEqual([r for r in res if r["status"] == "failed"], [])
        heads = [c[:4] for c in sh.calls]
        self.assertIn(["gcloud", "container", "clusters", "update"], heads)
        self.assertIn(["gcloud", "iam", "service-accounts", "create"], heads)
        self.assertEqual(sh.bindings, {("roles/iam.workloadIdentityUser", MEMBER), ("roles/iam.serviceAccountOpenIdTokenCreator", MEMBER)})
        self.assertEqual(sh.providers[0]["issuer_url"], ISSUER)
        kc = str(self.root / "kube" / "gke-a.kubeconfig")
        kube = [e for c, e in zip(sh.calls, sh.env_seen) if c[0] in ("kubectl", "helm")]
        self.assertTrue(kube and all(e.get("KUBECONFIG") == kc for e in kube))

    def test_existing_account_and_bindings_are_adopted(self):
        sh = GkeShell(gsa=True, bindings={("roles/iam.workloadIdentityUser", MEMBER), ("roles/iam.serviceAccountOpenIdTokenCreator", MEMBER)})
        by = {r["id"]: r for r in self.run_setup(sh, dry=False)}
        self.assertEqual(by["gsa"]["status"], "ok")
        writes = [c for c in sh.calls if c[:3] == ["gcloud", "iam", "service-accounts"] and c[3] in ("create", "add-iam-policy-binding")]
        self.assertEqual(writes, [])
        self.assertNotIn(["gcloud", "container", "clusters", "update"], [c[:4] for c in sh.calls])

    def test_dry_run_writes_nothing_to_google(self):
        sh = GkeShell(wi=False)
        by = {r["id"]: r for r in self.run_setup(sh, dry=True)}
        self.assertEqual(by["gsa"]["status"], "skipped")
        self.assertIn("would create", by["gsa"]["detail"])
        reads = ("describe", "get-iam-policy", "get-credentials")
        self.assertFalse([c for c in sh.calls if c[0] == "gcloud" and c[3] not in reads])

    def test_missing_auth_plugin_is_named(self):
        class NoPlugin(GkeShell):
            def run(self, argv, timeout=60.0, sensitive=False, extra_env=None):
                if argv[:1] == ["gke-gcloud-auth-plugin"]:
                    return Result(list(argv), 127, "", "not found", 0.0)
                return super().run(argv, timeout, sensitive, extra_env)
        res = self.run_setup(NoPlugin(), dry=True)
        self.assertEqual(res[-1]["id"], "kubeconfig")
        self.assertIn("gke-gcloud-auth-plugin", res[-1]["detail"])


if __name__ == "__main__":
    unittest.main()


class PluginPathTest(unittest.TestCase):
    """gcloud installs gke-gcloud-auth-plugin next to itself, often off PATH (PATH holds a symlink to
    gcloud): commands the console runs, and kubectl's exec plugin, must still find it."""

    def test_child_env_adds_gclouds_own_bin_dir(self):
        from kcp_console.shell import child_env
        with tempfile.TemporaryDirectory() as d:
            sdk, link = Path(d, "sdk", "bin"), Path(d, "link")
            sdk.mkdir(parents=True), link.mkdir()
            for name in ("gcloud", "gke-gcloud-auth-plugin"):
                (sdk / name).write_text("#!/bin/sh\n")
                (sdk / name).chmod(0o755)
            (link / "gcloud").symlink_to(sdk / "gcloud")
            env = child_env(base={"PATH": str(link)})
            self.assertEqual(env["PATH"].split(":"), [str(link), str(sdk.resolve())])
            self.assertEqual(child_env(base={"PATH": str(sdk)})["PATH"], str(sdk))   # already there: unchanged
            self.assertEqual(child_env(base={"PATH": "/nonexistent"})["PATH"], "/nonexistent")   # no gcloud
