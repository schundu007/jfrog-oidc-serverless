"""Direct execution (direct.py, runner kind "direct"): the plan the approval covers, DryRun
flags, idempotent steps, the guards, and that no token reaches a log or the run store."""

import json
import tempfile
import time
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

    def __init__(self, oidc=True, providers=None, mappings=None, pod_phase="Running", pod_reason="", ns_exists=True,
                 anonymous=False, downloaded_by=None):
        self.anonymous = anonymous
        self.installed = True
        self.downloaded_by = downloaded_by or model.default_config()["jfrog"]["mapped_user"]
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
        elif argv[:3] == ["kubectl", "get", "daemonset"] and not self.installed:
            out = json.dumps({"items": []})
        elif argv[:3] == ["kubectl", "get", "daemonset"]:
            out = json.dumps({"items": [{"metadata": {"name": "jfrog-cred-provider-jfrog-credential-provider-daemonset"},
                                         "status": {"desiredNumberScheduled": 2, "numberReady": 2}}]})
        elif argv[:3] == ["kubectl", "get", "pod"]:
            st = {"phase": self.pod_phase}
            if self.pod_reason:
                st["containerStatuses"] = [{"state": {"waiting": {"reason": self.pod_reason}}}]
            out = json.dumps({"metadata": {"uid": "pod-uid-2"}, "status": st})
        elif argv[:3] == ["kubectl", "get", "events"]:
            ok = self.pod_phase == "Running"
            # An earlier test pod with the same name left a failure behind; only uid pod-uid-2 is this run's.
            out = json.dumps({"items": [{"reason": "Failed", "message": "old 403 Forbidden", "involvedObject": {"uid": "pod-uid-1"}},
                                        {"reason": "Pulled" if ok else "Failed", "involvedObject": {"uid": "pod-uid-2"},
                                         "message": "Successfully pulled image" if ok else "401 Unauthorized"}]})
        elif argv[:3] == ["kubectl", "get", "serviceaccount"]:
            out = json.dumps({"metadata": {"name": argv[3], "annotations": {"JFrogExchange": "true"}}})
        elif argv[:2] == ["helm", "status"]:
            return Result(list(argv), 0 if self.installed else 1, "", "" if self.installed else "Error: release: not found", 0.0)
        elif argv[:3] == ["kubectl", "get", "nodes"]:
            out = json.dumps({"items": [{"metadata": {"name": "a"}}] if self.installed else []})
        elif argv[:2] == ["kubectl", "apply"]:
            out = "serviceaccount/jfrog-provider-sa created\n"
        if argv[:2] == ["helm", "uninstall"] and "--dry-run" not in argv:
            self.installed = False
        if argv[:2] == ["kubectl", "label"] and argv[-1].endswith("-"):
            self.installed = False
        return Result(list(argv), 0, out, "", 0.0)

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        self.http_calls.append((method, url, (headers or {}).get("Authorization", "")))
        if method == "GET" and url.endswith("/access/api/v1/oidc"):
            return HttpResult(method, url, 200, json.dumps(self.providers))
        if method == "GET" and url.endswith("/identity_mappings"):
            return HttpResult(method, url, 200, json.dumps(self.mappings))
        if method == "POST" and url.endswith("/access/api/v1/oidc"):
            self.providers.append(json.loads(body))
        elif method == "POST" and url.endswith("/identity_mappings"):
            self.mappings.append(json.loads(body))
        if method == "DELETE" and url.rstrip("/").split("/")[-2] == "oidc":
            self.providers = [p for p in self.providers if p.get("name") != url.rstrip("/").split("/")[-1]]
        if method == "GET" and "/v2/" in url:
            return HttpResult(method, url, 200 if self.anonymous else 401, "{}")
        if method == "GET" and url.endswith("?stats"):
            if "list.manifest.json" not in url:
                return HttpResult(method, url, 404, "{}")
            return HttpResult(method, url, 200, json.dumps({"downloadCount": 3, "lastDownloaded": int(time.time() * 1000),
                                                            "lastDownloadedBy": self.downloaded_by}))
        return HttpResult(method, url, 201 if method == "POST" else 204, "{}")


def set_up():
    """The JFrog side as Set up leaves it: the provider for ISSUER and the mapping for the service account."""
    c = model.merged(model.default_config())
    c["cluster"]["oidc_issuer"] = ISSUER
    return {"providers": [{"name": model.provider_name(c), "issuer_url": ISSUER, "provider_type": "Azure"}],
            "mappings": [model.mapping_request(c)]}


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
        self.assertIn("the namespace is created", next(r for r in res if r["id"] == "sa")["detail"])

    def test_dry_run_without_a_jfrog_token_still_checks_the_cluster(self):
        sh = FakeShell()
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=True, sleep=lambda s: None)
        res = rn.execute(direct.plan(cfg(), self.root, "setup", dry_run=True))
        self.assertFalse([r for r in res if r["status"] == "failed"], res)
        # Only the anonymous probe of the test image reaches JFrog, with no credentials.
        self.assertEqual([c for c in sh.http_calls if c[2] or "/v2/" not in c[1]], [])
        for i in ("provider", "mapping"):
            self.assertIn("no JFrog token", next(r for r in res if r["id"] == i)["detail"])
        self.assertIn("helm", [c[0] for c in sh.calls])

    def test_real_setup_without_a_jfrog_token_stops_at_jfrog(self):
        sh = FakeShell()
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=False, sleep=lambda s: None)
        res = rn.execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        self.assertEqual(res[-1]["id"], "provider")
        self.assertEqual(res[-1]["status"], "failed")

    def test_pull_reports_this_pods_events_only(self):
        res = self.runner(FakeShell(), dry=False).execute([{"id": "pull", "title": "pull"}])
        self.assertEqual((res[0]["status"], res[0]["detail"]), ("done", "pulled with no pull secret"))

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
        by = {r["id"]: r for r in res}
        self.assertEqual(by["pull"], {"id": "pull", "title": "Test pull without a pull secret", "status": "done", "detail": "pulled with no pull secret"})
        self.assertEqual(by["anon"]["status"], "ok")
        self.assertEqual((res[-1]["id"], res[-1]["status"]), ("who", "ok"))

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
        sh = FakeShell(pod_phase="Pending", pod_reason="ErrImagePull", **set_up())
        res = self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "verify", dry_run=False))
        self.assertEqual(res[-1]["status"], "failed")
        self.assertIn("ErrImagePull", res[-1]["detail"])
        self.assertIn("401 Unauthorized", res[-1]["detail"])

    def test_token_only_in_the_header(self):
        sh = FakeShell(oidc=False)
        self.runner(sh, dry=False).execute(direct.plan(cfg(), self.root, "setup", dry_run=False))
        self.assertTrue(all(a == f"Bearer {TOKEN}" for _, u, a in sh.http_calls if "/v2/" not in u))
        self.assertTrue(all(a == "" for _, u, a in sh.http_calls if "/v2/" in u))
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


class ConflictShell(FakeShell):
    """JFrog answers 409: the provider and mapping were already created (by Set up)."""

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        if method == "POST":
            self.http_calls.append((method, url, ""))
            return HttpResult(method, url, 409, '{"errors":[{"code":"CONFLICT","message":"already exists"}]}')
        return super().http(method, url, headers, body, timeout, sensitive)


class JfrogActionTest(unittest.TestCase):
    def manager(self, sh):
        root = Path(tempfile.mkdtemp())
        return RunManager(RunStore(root), lambda log: sh, env={"JFROG_ACCESS_TOKEN": TOKEN}, sleep=lambda s: None, root=root)

    def test_create_that_already_exists_succeeds(self):
        m = self.manager(ConflictShell())
        for name in ("create-provider", "create-mapping"):
            run = m.prepare(cfg(), "jfrog", name, {})
            m.approve(run["id"], run["request_sha256"], approved=True)
            done = m.wait(run["id"])
            self.assertEqual(done["status"], "succeeded", name)
            self.assertEqual(done["result"]["status"], 409)

    def test_promote_needs_a_source_repo(self):
        r = build_request(cfg(jfrog={"promote_source_repo": ""}), "jfrog", "promote", {}, root="/tmp/x")
        self.assertTrue([g for g in r["guards"] if "promote_source_repo" in g], r["guards"])


class DiscoverKubeconfigTest(unittest.TestCase):
    """Discover reads the configured cluster through the console's kubeconfig, never the operator's
    current kubectl context (which may be another cluster)."""

    def test_reads_use_the_clusters_kubeconfig(self):
        from kcp_console.checks import preflight
        root = Path(tempfile.mkdtemp())
        sh = FakeShell()
        kc = direct.paths(root, "sandbox-pilot")["kubeconfig"]
        preflight.discover(cfg(), sh, env={}, kubeconfig=kc)
        reads = [(c, e) for c, e in zip(sh.calls, sh.env_seen) if c[0] in ("kubectl", "helm")]
        self.assertTrue(reads)
        for c, e in reads:
            self.assertEqual(e.get("KUBECONFIG"), str(kc), c)
        self.assertIn(["az", "aks", "get-credentials"], [c[:3] for c in sh.calls])
        self.assertTrue(kc.exists())


class DiscoverStoreTest(unittest.TestCase):
    def test_secret_looking_manifest_is_redacted_not_refused(self):
        from kcp_console.store import StatusStore
        st = StatusStore(Path(tempfile.mkdtemp()))
        from kcp_console import redact
        facts = {"manifest": "data:\n  token: eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2lnbmF0dXJl\n"}
        st.update("sandbox-pilot", {"discover": redact.obj(facts)})
        self.assertNotIn("eyJhbGci", json.dumps(st.get("sandbox-pilot")))


class ProofTest(unittest.TestCase):
    """Verify proves the OIDC path link by link; each broken link names itself."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def verify(self, sh):
        rn = direct.Runner(cfg(), sh, self.root, {"JFROG_ACCESS_TOKEN": TOKEN}, lambda line: None, dry_run=False, sleep=lambda s: None)
        return {r["id"]: r for r in rn.execute(direct.plan(cfg(), self.root, "verify", dry_run=False))}

    def test_every_link_holds(self):
        by = self.verify(FakeShell(**set_up()))
        self.assertEqual([i for i in direct.VERIFY_STEPS if by[i]["status"] not in ("ok", "done")], [])
        self.assertIn("jfrog-provider-sa →", by["map"]["detail"])
        self.assertIn("401", by["anon"]["detail"])
        self.assertNotIn("cached", by["who"]["detail"])

    def test_anonymous_access_is_a_warning(self):
        by = self.verify(FakeShell(anonymous=True, **set_up()))
        self.assertEqual(by["anon"]["status"], "warn")
        self.assertIn("proves nothing", by["anon"]["detail"])

    def test_pull_by_another_user_fails(self):
        by = self.verify(FakeShell(downloaded_by="admin", **set_up()))
        self.assertEqual(by["who"]["status"], "failed")
        self.assertIn("not ", by["who"]["detail"])

    def test_missing_mapping_stops_before_the_pull(self):
        by = self.verify(FakeShell(providers=set_up()["providers"]))
        self.assertEqual(by["map"]["status"], "failed")
        self.assertNotIn("pull", by)

    def test_cached_image_keeps_the_earlier_download(self):
        sh = FakeShell(**set_up())
        rn = direct.Runner(cfg(), sh, self.root, {"JFROG_ACCESS_TOKEN": TOKEN}, lambda line: None, dry_run=False,
                           sleep=lambda s: None, clock=lambda: time.time() + 3600)
        by = {r["id"]: r for r in rn.execute(direct.plan(cfg(), self.root, "verify", dry_run=False))}
        self.assertEqual(by["who"]["status"], "ok")
        self.assertIn("cached", by["who"]["detail"])

    def test_results_name_no_issuer_image_or_ids(self):
        by = self.verify(FakeShell(**set_up()))
        text = " ".join(r["detail"] for r in by.values())
        self.assertNotIn(ISSUER, text)
        self.assertNotIn("https://", text)
        self.assertNotIn(model.default_config()["jfrog"]["test_image"], text)

    def test_tear_down_ends_with_evidence(self):
        sh = FakeShell(**set_up())
        rn = direct.Runner(cfg(), sh, self.root, {"JFROG_ACCESS_TOKEN": TOKEN}, lambda line: None, dry_run=False, sleep=lambda s: None)
        res = rn.execute(direct.plan(cfg(), self.root, "teardown", dry_run=False))
        self.assertEqual((res[-1]["id"], res[-1]["status"]), ("gone", "ok"), res)

    def test_tear_down_names_what_is_left(self):
        sh = FakeShell(**set_up())
        rn = direct.Runner(cfg(), sh, self.root, {"JFROG_ACCESS_TOKEN": TOKEN}, lambda line: None, dry_run=False, sleep=lambda s: None)
        res = rn.execute([{"id": "gone", "title": "gone"}])
        self.assertEqual(res[0]["status"], "failed")
        self.assertIn("Helm release", res[0]["detail"])
        self.assertIn("JFrog OIDC provider", res[0]["detail"])

    def test_proof_steps_are_in_verify(self):
        self.assertTrue(set(direct.PROOF_STEPS) <= set(direct.VERIFY_STEPS))


if __name__ == "__main__":
    unittest.main()
