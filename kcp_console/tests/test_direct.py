"""Direct execution (direct.py, runner kind "direct"): the plan the approval covers, DryRun
flags, idempotent steps, the guards, and that no token reaches a log or the run store."""

import json
import tempfile
import unittest
from pathlib import Path

from kcp_console import direct, model
from kcp_console.runner import RunError, RunManager, build_request
from kcp_console.shell import HttpResult, Result
from kcp_console.store import RunStore

TOKEN = "jfrog-access-token-under-test"
ISSUER = model.SANDBOX_ISSUER


class FakeShell:
    """Answers az/kubectl/helm and the Access API like a cluster with the provider not yet set up."""

    def __init__(self, oidc=True, providers=None, mappings=None, pod_phase="Running", pod_reason="", ns_exists=True):
        self.calls, self.http_calls = [], []
        self.ns_exists = ns_exists
        self.oidc, self.providers, self.mappings = oidc, providers or [], mappings or []
        self.pod_phase, self.pod_reason = pod_phase, pod_reason
        self.env_seen = []

    def run(self, argv, timeout=60.0, sensitive=False, extra_env=None):
        self.calls.append(list(argv))
        self.env_seen.append(extra_env or {})
        out = ""
        if argv[:3] == ["az", "aks", "show"]:
            out = json.dumps({"kubernetesVersion": "1.30.4", "oidcIssuerProfile": {"enabled": self.oidc, "issuerUrl": ISSUER if self.oidc else None},
                              "securityProfile": {"workloadIdentity": {"enabled": True}}})
        elif argv[:3] == ["az", "aks", "update"]:
            self.oidc = True
        elif argv[:3] == ["az", "aks", "get-credentials"]:
            Path(argv[argv.index("--file") + 1]).write_text("apiVersion: v1\nkind: Config\n")
        elif argv[:3] == ["kubectl", "get", "namespace"]:
            if not self.ns_exists:
                return Result(list(argv), 1, "", f'Error from server (NotFound): namespaces "{argv[3]}" not found', 0.0)
            out = json.dumps({"metadata": {"name": argv[3]}})
        elif argv[:2] == ["kubectl", "label"]:
            out = "node/a labeled\nnode/b labeled\n"
        elif argv[:3] == ["kubectl", "get", "daemonset"]:
            out = json.dumps({"items": [{"metadata": {"name": "jfrog-cred-provider-jfrog-credential-provider-daemonset"},
                                         "status": {"desiredNumberScheduled": 2, "numberReady": 2}}]})
        elif argv[:3] == ["kubectl", "get", "pod"]:
            st = {"phase": self.pod_phase}
            if self.pod_reason:
                st["containerStatuses"] = [{"state": {"waiting": {"reason": self.pod_reason}}}]
            out = json.dumps({"status": st})
        elif argv[:3] == ["kubectl", "get", "events"]:
            ok = self.pod_phase == "Running"
            out = json.dumps({"items": [{"reason": "Pulled" if ok else "Failed",
                                         "message": "Successfully pulled image" if ok else "401 Unauthorized"}]})
        elif argv[:2] == ["kubectl", "apply"]:
            out = "serviceaccount/jfrog-provider-sa created\n"
        return Result(list(argv), 0, out, "", 0.0)

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        self.http_calls.append((method, url, (headers or {}).get("Authorization", "")))
        if method == "GET" and url.endswith("/access/api/v1/oidc"):
            return HttpResult(method, url, 200, json.dumps(self.providers))
        if method == "GET" and url.endswith("/identity_mappings"):
            return HttpResult(method, url, 200, json.dumps(self.mappings))
        return HttpResult(method, url, 201 if method == "POST" else 204, "{}")


def cfg(**over):
    c = model.default_config()
    c["cluster"]["oidc_issuer"] = ""
    for k, v in over.items():
        if isinstance(v, dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_setup_steps_follow_azure_md(self):
        steps = direct.plan(cfg(), self.root, "setup", dry_run=True)
        self.assertEqual([s["id"] for s in steps], list(direct.STEPS))
        by = {s["id"]: s for s in steps}
        self.assertIn("--dry-run=server", by["helm"]["command"])
        self.assertIn("--repo https://charts.jfrog.io", by["helm"]["command"])
        self.assertIn("--dry-run=server", by["sa"]["command"])
        self.assertIn("--enable-oidc-issuer", by["oidc"]["command"])
        for i in ("oidc", "provider", "mapping"):
            self.assertTrue(by[i]["dry_run_note"], i)

    def test_real_run_has_no_dry_run_flags(self):
        steps = direct.plan(cfg(), self.root, "setup", dry_run=False)
        self.assertFalse(any("--dry-run" in s["command"] for s in steps))
        self.assertFalse(any(s["dry_run_note"] for s in steps))

    def test_problems_name_missing_inputs(self):
        c = model.blank_config("blank")
        self.assertIn("cluster.name is empty", direct.problems(c))
        self.assertIn("jfrog.mapping_name is empty", direct.problems(c))
        self.assertNotIn("jfrog.host is empty", direct.problems(c, "verify"))

    def test_sa_and_pod_manifests(self):
        sa = direct.sa_manifest(model.merged(cfg()))["items"][1]
        self.assertEqual(sa["metadata"]["annotations"], {"JFrogExchange": "true"})
        pod = direct.pod_manifest(model.merged(cfg()))
        self.assertEqual(pod["spec"]["imagePullSecrets"], [])
        self.assertEqual(pod["spec"]["serviceAccountName"], "jfrog-provider-sa")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.lines = []

    def runner(self, sh, dry, c=None):
        return direct.Runner(c or cfg(), sh, self.root, {"JFROG_ACCESS_TOKEN": TOKEN}, self.lines.append, dry_run=dry, sleep=lambda s: None)

    def test_dry_run_changes_nothing(self):
        sh = FakeShell(oidc=False)
        res = self.runner(sh, dry=True).execute(direct.plan(cfg(), self.root, "setup", dry_run=True))
        self.assertEqual(len(res), len(direct.STEPS), res)
        self.assertFalse([r for r in res if r["status"] == "failed"], res)
        self.assertNotIn(["az", "aks", "update"], [c[:3] for c in sh.calls])
        self.assertFalse([m for m, _, _ in sh.http_calls if m != "GET"])
        for c in sh.calls:
            if (c[0] == "kubectl" and c[1] in ("apply", "label")) or c[0] == "helm":
                self.assertIn("--dry-run=server", c, c)
        values = (self.root / "kube" / "sandbox-pilot-values.yaml").read_text()
        self.assertIn('serviceAccountTokenAudience: "api://AzureADTokenExchange"', values)

    def test_dry_run_on_a_fresh_cluster_validates_namespaced_objects_client_side(self):
        # A server-side dry run cannot place the service account or the test pod in a namespace it only
        # pretends to create, so on a cluster without the namespace those two are checked client-side.
        sh = FakeShell(ns_exists=False)
        res = self.runner(sh, dry=True).execute(direct.plan(cfg(), self.root, "setup", dry_run=True))
        self.assertFalse([r for r in res if r["status"] == "failed"], res)
        applies = [c for c in sh.calls if c[:2] == ["kubectl", "apply"]]
        self.assertEqual(len(applies), 2)
        for c in applies:
            self.assertIn("--dry-run=client", c)
        self.assertIn("namespace jfrog is created", next(r for r in res if r["id"] == "sa")["detail"])

    def test_dry_run_without_a_jfrog_token_still_checks_the_cluster(self):
        sh = FakeShell()
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=True, sleep=lambda s: None)
        res = rn.execute(direct.plan(cfg(), self.root, "setup", dry_run=True))
        self.assertFalse([r for r in res if r["status"] == "failed"], res)
        self.assertEqual(sh.http_calls, [])
        for i in ("provider", "mapping"):
            self.assertIn("no JFrog token", next(r for r in res if r["id"] == i)["detail"])
        self.assertIn("helm", [c[0] for c in sh.calls])

    def test_real_setup_without_a_jfrog_token_stops_at_jfrog(self):
        sh = FakeShell()
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=False, sleep=lambda s: None)
        res = rn.execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        self.assertEqual(res[-1]["id"], "provider")
        self.assertEqual(res[-1]["status"], "failed")

    def test_real_setup_creates_what_is_missing(self):
        sh = FakeShell(oidc=False)
        res = self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        self.assertEqual([r for r in res if r["status"] == "failed"], [])
        self.assertIn(["az", "aks", "update"], [c[:3] for c in sh.calls])
        posts = [u for m, u, _ in sh.http_calls if m == "POST"]
        self.assertEqual(posts, ["https://example.jfrog.io/access/api/v1/oidc",
                                 "https://example.jfrog.io/access/api/v1/oidc/aks-sandbox-oidc/identity_mappings"])
        kc = str(self.root / "kube" / "sandbox-pilot.kubeconfig")
        kube = [e for c, e in zip(sh.calls, sh.env_seen) if c[0] in ("kubectl", "helm")]
        self.assertTrue(kube and all(e.get("KUBECONFIG") == kc for e in kube))
        self.assertEqual(res[-1], {"id": "pull", "status": "done", "detail": "pulled with imagePullSecrets: [] — Successfully pulled image"})

    def test_existing_provider_and_mapping_are_left_alone(self):
        sh = FakeShell(providers=[{"name": "aks-sandbox-oidc", "issuer_url": ISSUER}], mappings=[{"name": "aks-workload-identity-mapping"}])
        res = self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        by = {r["id"]: r for r in res}
        self.assertEqual((by["provider"]["status"], by["mapping"]["status"]), ("ok", "ok"))
        self.assertFalse([m for m, _, _ in sh.http_calls if m == "POST"])

    def test_provider_with_another_issuer_stops_the_plan(self):
        sh = FakeShell(providers=[{"name": "aks-sandbox-oidc", "issuer_url": "https://other.example/issuer/"}])
        res = self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        self.assertEqual(res[-1]["id"], "provider")
        self.assertEqual(res[-1]["status"], "failed")
        self.assertNotIn("helm", [c[0] for c in sh.calls])

    def test_failed_pull_reports_the_event(self):
        sh = FakeShell(pod_phase="Pending", pod_reason="ErrImagePull")
        res = self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "verify", dry_run=False))
        self.assertEqual(res[-1]["status"], "failed")
        self.assertIn("ErrImagePull", res[-1]["detail"])
        self.assertIn("401 Unauthorized", res[-1]["detail"])

    def test_token_only_in_the_header(self):
        sh = FakeShell(oidc=False)
        self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        self.assertTrue(all(a == f"Bearer {TOKEN}" for _, _, a in sh.http_calls))
        self.assertFalse([c for c in sh.calls if TOKEN in " ".join(c)])
        self.assertFalse([line for line in self.lines if TOKEN in line])
        for f in (self.root / "kube").iterdir():
            self.assertNotIn(TOKEN, f.read_text(), f.name)


class RequestTest(unittest.TestCase):
    def test_dry_run_is_default_and_read_only(self):
        r = build_request(cfg(), "direct", "setup", {}, root="/tmp/x")
        self.assertIs(r["request"]["parameters"]["DryRun"], True)
        self.assertFalse(r["changes_state"])
        self.assertEqual(r["guards"], [])

    def test_hash_binds_the_commands(self):
        a = build_request(cfg(), "direct", "setup", {"DryRun": True}, root="/tmp/x")
        b = build_request(cfg(), "direct", "setup", {"DryRun": False}, root="/tmp/x")
        c = build_request(cfg(k8s={"release": "other"}), "direct", "setup", {"DryRun": False}, root="/tmp/x")
        self.assertEqual(len({a["request_sha256"], b["request_sha256"], c["request_sha256"]}), 3)
        self.assertTrue(b["changes_state"])

    def test_teardown_needs_the_typed_cluster(self):
        r = build_request(cfg(), "direct", "teardown", {"DryRun": False}, root="/tmp/x")
        self.assertTrue(r["needs_typed_cluster"])

    def test_guards(self):
        prot = model.default_config()["jfrog"]["protected_providers"][0]
        r = build_request(cfg(values={"provider_name": prot}), "direct", "setup", {}, root="/tmp/x")
        self.assertTrue(any("protected" in g for g in r["guards"]))
        r = build_request(cfg(demo_mode=True), "direct", "setup", {"DryRun": False}, root="/tmp/x")
        self.assertTrue(any("Demo mode" in g for g in r["guards"]))
        with self.assertRaises(RunError):
            build_request(cfg(), "direct", "nuke", {}, root="/tmp/x")


class ManagerTest(unittest.TestCase):
    def test_prepare_approve_run_and_store_without_token(self):
        root = Path(tempfile.mkdtemp())
        sh = FakeShell()
        m = RunManager(RunStore(root), lambda log: sh, env={"JFROG_ACCESS_TOKEN": TOKEN}, sleep=lambda s: None, root=root)
        run = m.prepare(cfg(), "direct", "setup", {"DryRun": False})
        with self.assertRaises(RunError):
            m.approve(run["id"], run["request_sha256"], approved=False)
        m.approve(run["id"], run["request_sha256"], approved=True)
        done = m.wait(run["id"])
        self.assertEqual(done["status"], "succeeded", done.get("events"))
        self.assertEqual(len(done["result"]["steps"]), len(direct.STEPS))
        stored = "".join(p.read_text() for p in root.rglob("*") if p.is_file())
        self.assertNotIn(TOKEN, stored)


if __name__ == "__main__":
    unittest.main()
