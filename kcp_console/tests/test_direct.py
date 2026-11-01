"""Direct execution (direct.py, runner kind "direct"): the plan the approval covers, DryRun
flags, idempotent steps, the guards, and that no token reaches a log or the run store."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from kcp_console import direct, model
from kcp_console.jfrog import StaticToken
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

    def run(self, argv, timeout=60.0, sensitive=False, extra_env=None, stream=False):
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


class CauseTest(unittest.TestCase):
    def test_token_fetch_401_names_the_cached_credentials(self):
        ev = 'Failed to pull image: failed to fetch oauth token: unexpected status from GET request to https://x/token: 401 Unauthorized'
        self.assertEqual(direct._cause(ev), "registry token request got 401: cached or expired credentials")

    def test_plain_401_stays_short(self):
        self.assertEqual(direct._cause("pull access denied: 401 Unauthorized"), "401 Unauthorized")


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.lines = []

    def runner(self, sh, dry, c=None):
        return direct.Runner(c or cfg(), sh, self.root, {}, self.lines.append, dry_run=dry, sleep=lambda s: None, tokens=StaticToken(TOKEN))

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
        # The provider pods restart after the upgrade, so nodes pick up the new config and kubelet drops cached tokens.
        self.assertIn(["kubectl", "rollout", "restart"], [c[:3] for c in sh.calls])
        posts = [u for m, u, _ in sh.http_calls if m == "POST"]
        self.assertEqual(posts, ["https://example.jfrog.io/access/api/v1/oidc",
                                 "https://example.jfrog.io/access/api/v1/oidc/aks-sandbox-oidc/identity_mappings"])
        kc = str(self.root / "kube" / "sandbox-pilot.kubeconfig")
        kube = [e for c, e in zip(sh.calls, sh.env_seen) if c[0] in ("kubectl", "helm")]
        self.assertTrue(kube and all(e.get("KUBECONFIG") == kc for e in kube))
        by = {r["id"]: r for r in res}
        self.assertEqual({k: v for k, v in by["pull"].items() if k != "facts"},
                         {"id": "pull", "title": "Test pull without a pull secret", "status": "done", "detail": "pulled with no pull secret"})
        self.assertEqual([e["reason"] for e in by["pull"]["facts"]["events"]], ["Pulled"])
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
        r = build_request(cfg(), "direct", "verify", {}, root="/tmp/x")
        self.assertIs(r["request"]["parameters"]["DryRun"], True)
        self.assertFalse(r["changes_state"])
        self.assertEqual(r["guards"], [])

    def test_aks_install_and_removal_belong_to_the_pipeline(self):
        # The aks-jfrog-trust stack-setup pipeline installs and removes the provider; the console
        # refuses both, dry or not, and still verifies and proves.
        for name in ("setup", "teardown"):
            for dry in (True, False):
                r = build_request(cfg(), "direct", name, {"DryRun": dry}, root="/tmp/x")
                self.assertTrue(any("aks-jfrog-trust" in g for g in r["guards"]), (name, dry))
        for name in ("verify", "prove"):
            r = build_request(cfg(), "direct", name, {"DryRun": False}, root="/tmp/x")
            self.assertFalse(any("aks-jfrog-trust" in g for g in r["guards"]), name)

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
        sh = FakeShell(**set_up())
        m = RunManager(RunStore(root), lambda log: sh, env={}, sleep=lambda s: None, root=root, tokens=StaticToken(TOKEN))
        refused = m.prepare(cfg(), "direct", "setup", {"DryRun": False})   # AKS: the pipeline installs
        self.assertEqual(refused["status"], "refused")
        run = m.prepare(cfg(), "direct", "verify", {"DryRun": False})
        with self.assertRaises(RunError):
            m.approve(run["id"], run["request_sha256"], approved=False)
        m.approve(run["id"], run["request_sha256"], approved=True)
        done = m.wait(run["id"])
        self.assertEqual(done["status"], "succeeded", done.get("events"))
        self.assertEqual(len(done["result"]["steps"]), len(direct.VERIFY_STEPS))
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
        return RunManager(RunStore(root), lambda log: sh, env={}, sleep=lambda s: None, root=root, tokens=StaticToken(TOKEN))

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
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=False, sleep=lambda s: None, tokens=StaticToken(TOKEN))
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
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=False,
                           sleep=lambda s: None, clock=lambda: time.time() + 3600, tokens=StaticToken(TOKEN))
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
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=False, sleep=lambda s: None, tokens=StaticToken(TOKEN))
        res = rn.execute(direct.plan(cfg(), self.root, "teardown", dry_run=False))
        self.assertEqual((res[-1]["id"], res[-1]["status"]), ("gone", "ok"), res)

    def test_tear_down_names_what_is_left(self):
        sh = FakeShell(**set_up())
        rn = direct.Runner(cfg(), sh, self.root, {}, lambda line: None, dry_run=False, sleep=lambda s: None, tokens=StaticToken(TOKEN))
        res = rn.execute([{"id": "gone", "title": "gone"}])
        self.assertEqual(res[0]["status"], "failed")
        self.assertIn("Helm release", res[0]["detail"])
        self.assertIn("JFrog OIDC provider", res[0]["detail"])

    def test_proof_steps_are_in_verify(self):
        self.assertTrue(set(direct.PROOF_STEPS) <= set(direct.VERIFY_STEPS))


if __name__ == "__main__":
    unittest.main()


class PruneTest(unittest.TestCase):
    """Clear old runs: the newest finished run of each action per cluster stays (it is the cluster's evidence)."""

    def test_keeps_newest_per_action_and_active_runs(self):
        root = Path(tempfile.mkdtemp())
        store = RunStore(root)
        m = RunManager(store, lambda log: FakeShell(), env={}, sleep=lambda s: None, root=root, tokens=StaticToken(TOKEN))
        rows = [("20261030-100000-aaaaa1", "aks-1", "Verify", "failed"), ("20261030-110000-aaaaa2", "aks-1", "Verify", "succeeded"),
                ("20261030-120000-aaaaa3", "aks-1", "setup", "succeeded"), ("20261030-130000-aaaaa4", "aks-1", "setup", "running"),
                ("20261030-140000-aaaaa5", "aks-1", "setup", "refused"), ("20261030-150000-aaaaa6", "aks-2", "Verify", "failed")]
        for rid, conf, name, status in rows:
            run = {"id": rid, "created": f"2026-10-30T{rid[9:11]}:00:00Z", "config": conf, "kind": "direct", "name": name, "status": status}
            m.runs[rid] = run
            store.save(run)
        self.assertEqual(m.prune("aks-1"), {"runs_deleted": 2, "runs_kept": 3})
        self.assertEqual(sorted(m.runs), ["20261030-110000-aaaaa2", "20261030-120000-aaaaa3", "20261030-130000-aaaaa4",
                                          "20261030-150000-aaaaa6"])
        self.assertFalse((root / "runs" / "20261030-100000-aaaaa1").exists())
        self.assertTrue((root / "runs" / "20261030-110000-aaaaa2").exists())

    def test_delete_one_finished_run_refuses_active(self):
        root = Path(tempfile.mkdtemp())
        store = RunStore(root)
        m = RunManager(store, lambda log: FakeShell(), env={}, sleep=lambda s: None, root=root, tokens=StaticToken(TOKEN))
        for rid, status in (("20261030-100000-bbbbb1", "failed"), ("20261030-110000-bbbbb2", "prepared"),
                            ("20261030-120000-bbbbb3", "running")):
            run = {"id": rid, "created": "2026-10-30T10:00:00Z", "config": "aks-1", "kind": "direct", "name": "setup", "status": status}
            m.runs[rid] = run
            store.save(run)
        self.assertEqual(m.delete("20261030-100000-bbbbb1"), {"deleted": "20261030-100000-bbbbb1"})
        self.assertFalse((root / "runs" / "20261030-100000-bbbbb1").exists())
        for rid in ("20261030-110000-bbbbb2", "20261030-120000-bbbbb3"):
            with self.assertRaises(RunError) as cm:
                m.delete(rid)
            self.assertEqual(cm.exception.status, 409)
            self.assertIn(rid, m.runs)
        with self.assertRaises(RunError) as cm:
            m.delete("20261030-130000-bbbbb4")
        self.assertEqual(cm.exception.status, 404)


SA_JWT, ISSUED = "eyJsa-jwt.payload.sig", "eyJissued-jfrog.payload.sig"


class TrustShell(FakeShell):
    """A cluster where the pull works only while JFrog holds the mapping, as with a live exchange.
    cached=True: kubelet reused a credential, so the pull works without the trust (the proof must catch it)."""

    def __init__(self, cached=False, tokens=None, pull_secrets=0, **kw):
        super().__init__(**kw)
        self.cached, self.tokens, self.pull_secrets = cached, tokens or [], pull_secrets
        self.sa_deletes = 0

    def run(self, argv, timeout=60.0, sensitive=False, extra_env=None, stream=False):
        if argv[:3] == ["kubectl", "get", "pod"]:
            self.pod_phase, self.pod_reason = ("Running", "") if (self.mappings or self.cached) else ("Pending", "ErrImagePull")
        if argv[:3] == ["kubectl", "get", "secrets"]:
            self.calls.append(list(argv))
            return Result(list(argv), 0, "kubernetes.io/service-account-token\n" + "kubernetes.io/dockerconfigjson\n" * self.pull_secrets, "", 0.0)
        if argv[:3] == ["kubectl", "delete", "serviceaccount"]:
            self.sa_deletes += 1
        if argv[:3] == ["kubectl", "create", "token"]:
            self.calls.append(list(argv))
            return Result(list(argv), 0, SA_JWT + "\n", "", 0.0)
        return super().run(argv, timeout, sensitive, extra_env, stream)

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        if method == "GET" and url.endswith("/access/api/v1/tokens"):
            self.http_calls.append((method, url, (headers or {}).get("Authorization", "")))
            return HttpResult(method, url, 200, json.dumps({"tokens": self.tokens}))
        if method == "POST" and url.endswith("/access/api/v1/oidc/token"):
            assert (headers or {}).get("Content-Type") == "application/json" and "Authorization" not in (headers or {})
            self.http_calls.append((method, url, json.loads(body)["subject_token"]))
            if self.mappings:
                return HttpResult(method, url, 200, json.dumps({"access_token": ISSUED, "username": "kcp-pull-user", "expires_in": 3600}))
            return HttpResult(method, url, 401, json.dumps({"errors": [{"code": "UNAUTHORIZED", "message": "Failed to find identity mapping"}]}))
        if method == "DELETE" and "/identity_mappings/" in url:
            name = url.rsplit("/", 1)[-1]
            self.mappings = [m for m in self.mappings if m.get("name") != name]
        return super().http(method, url, headers, body, timeout, sensitive)


class PluginRowsTest(unittest.TestCase):
    LOG = "\n".join(json.dumps(x) for x in [
        {"time": "2026-11-01T08:45:20.1Z", "level": "INFO", "msg": "Running JFrog Credentials provider..."},
        {"time": "2026-11-01T08:46:16.590133517Z", "level": "INFO", "msg": "Running JFrog Credentials provider..."},
        {"time": "2026-11-01T08:46:16.59Z", "level": "INFO", "msg": "Fetching environment variable:cloud_provider"},
        {"time": "2026-11-01T08:46:16.6Z", "level": "INFO", "msg": "RT oidc token url :https://acme.jfrog.io/access/api/v1/oidc/token"},
        {"time": "2026-11-01T08:46:26.7Z", "level": "ERROR", "msg": "token creation to https://acme.jfrog.io/access/api/v1/oidc/token?x=1 returned 403 eyJabc.def.ghi"},
    ]) + "\nnot json"

    def test_window_noise_urls_and_tokens(self):
        rows = direct.plugin_rows(self.LOG, direct._epoch("2026-11-01T08:46:00Z"))
        self.assertEqual([r["level"] for r in rows], ["INFO", "INFO", "ERROR"])
        self.assertEqual(rows[0]["at"], "2026-11-01T08:46:16Z")
        self.assertEqual(rows[1]["message"], "RT oidc token url :acme.jfrog.io/access/api/v1/oidc/token")
        self.assertIn("acme.jfrog.io/access/api/v1/oidc/token returned 403", rows[2]["message"])
        self.assertNotIn("eyJabc", json.dumps(rows))


class ProveTest(unittest.TestCase):
    """Prove: no stored secret, a live pull, refused without the trust, and the trust always restored."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.lines = []

    def prove(self, sh, dry=False, stop=lambda: False):
        rn = direct.Runner(cfg(), sh, self.root, {}, self.lines.append, dry_run=dry, sleep=lambda s: None, tokens=StaticToken(TOKEN))
        return {r["id"]: r for r in rn.execute(direct.plan(cfg(), self.root, "prove", dry_run=dry), stop=stop)}

    def test_every_claim_holds(self):
        sh = TrustShell(tokens=[{"subject": "jfac@x/users/kcp-pull-user", "issued_at": 100, "expiry": 3700}], **set_up())
        by = self.prove(sh)
        self.assertEqual([i for i in direct.PROVE_STEPS if by[i]["status"] not in ("ok", "done")], [], by)
        self.assertEqual(by["no-secret"]["facts"]["pull_secrets"], 0)
        self.assertEqual(by["no-keys"]["facts"], {"long_lived": 0, "short_lived": 1})
        self.assertFalse(by["refused"]["facts"]["pulled"])
        self.assertTrue(by["repull"]["facts"]["pulled"])
        self.assertEqual([e["message"] for e in by["refused"]["facts"]["events"]], ["401 Unauthorized"])
        self.assertEqual([e["reason"] for e in by["repull"]["facts"]["events"]], ["Pulled"])
        pod = by["refused"]["facts"]["pod"]
        self.assertEqual({k: pod[k] for k in ("namespace", "pod")}, {"namespace": "jfrog", "pod": "secretless-pull-test"})
        self.assertTrue(pod["cluster"] and pod["platform"] and pod["read_at"].endswith("Z"))
        self.assertEqual(len(sh.mappings), 1)            # the trust is back
        self.assertEqual(sh.sa_deletes, 3)
        self.assertEqual(by["denied"]["facts"]["jfrog"]["status"], 401)
        self.assertEqual(by["denied"]["facts"]["jfrog"]["answer"], "Failed to find identity mapping")
        self.assertEqual(by["granted-2"]["facts"]["jfrog"]["answer"], "token issued for kcp-pull-user, expires in 60 min")
        self.assertEqual(by["granted-1"]["facts"]["jfrog"]["subject"], "system:serviceaccount:jfrog:jfrog-provider-sa")
        out = "\n".join(self.lines) + json.dumps(by)
        for secret in (TOKEN, SA_JWT, ISSUED):
            self.assertNotIn(secret, out)

    def test_a_cached_credential_fails_the_proof_and_still_restores(self):
        sh = TrustShell(cached=True, **set_up())
        by = self.prove(sh)
        self.assertEqual(by["refused"]["status"], "failed")
        self.assertIn("does not depend on OIDC", by["refused"]["detail"])
        self.assertEqual(by["restore"]["status"], "done")
        self.assertNotIn("repull", by)
        self.assertEqual(len(sh.mappings), 1)

    def test_a_stop_after_the_break_still_restores(self):
        sh = TrustShell(**set_up())
        seen = []
        by = self.prove(sh, stop=lambda: seen.append(1) or len(seen) > 14)
        self.assertIn("break", by)
        self.assertEqual(by["restore"]["status"], "done")
        self.assertEqual(len(sh.mappings), 1)

    def test_stored_secrets_fail_the_claim(self):
        by = self.prove(TrustShell(pull_secrets=1, **set_up()))
        self.assertEqual(by["no-secret"]["status"], "failed")
        self.assertNotIn("pull", by)
        self.assertEqual(by["restore"]["status"], "skipped")

    def test_a_long_lived_token_fails_the_claim(self):
        by = self.prove(TrustShell(tokens=[{"subject": "jfac@x/users/kcp-pull-user", "issued_at": 100}], **set_up()))
        self.assertEqual(by["no-keys"]["status"], "failed")

    def test_dry_run_changes_nothing(self):
        sh = TrustShell(**set_up())
        by = self.prove(sh, dry=True)
        self.assertFalse([r for r in by.values() if r["status"] == "failed"], by)
        self.assertEqual(sh.sa_deletes, 0)
        self.assertFalse([m for m, _, _ in sh.http_calls if m != "GET"])
        self.assertEqual(len(sh.mappings), 1)

    def test_request_warns_and_is_not_destructive(self):
        r = build_request(cfg(), "direct", "prove", {"DryRun": False}, root=str(self.root))
        self.assertTrue(r["changes_state"])
        self.assertFalse(r["needs_typed_cluster"])
        self.assertTrue(any("restores the mapping" in w for w in r["warnings"]))


class EventRowsTest(unittest.TestCase):
    def test_the_pods_own_events_oldest_first_with_urls_cut_to_the_host(self):
        items = [
            {"involvedObject": {"uid": "u1"}, "lastTimestamp": "2026-11-01T08:27:20Z", "type": "Warning", "reason": "Failed",
             "source": {"component": "kubelet", "host": "aks-node-0"}, "count": 2,
             "message": 'failed to fetch anonymous token: unexpected status from GET request to https://acme.jfrog.io/v2/token?scope=repository:x:pull&service=y: 401 Unauthorized'},
            {"involvedObject": {"uid": "u1"}, "lastTimestamp": "2026-11-01T08:27:18Z", "type": "Normal", "reason": "Pulling",
             "source": {"component": "kubelet", "host": "aks-node-0"}, "message": 'Pulling image "acme.jfrog.io/docker/app:1"'},
            {"involvedObject": {"uid": "old"}, "lastTimestamp": "2026-11-01T08:00:00Z", "reason": "Pulled", "message": "earlier pod"},
        ]
        rows = direct.event_rows(items, "u1")
        self.assertEqual([r["reason"] for r in rows], ["Pulling", "Failed"])
        self.assertEqual(rows[1]["from"], "kubelet on aks-node-0")
        self.assertEqual(rows[1]["count"], 2)
        self.assertIn("request to acme.jfrog.io: 401 Unauthorized", rows[1]["message"])
        self.assertNotIn("https://", rows[1]["message"])
        self.assertNotIn("scope=", rows[1]["message"])


class WhyTest(unittest.TestCase):
    def test_the_failed_to_pull_event_names_the_401_not_the_backoff_after_it(self):
        msgs = ['Failed to pull image "h/x:1": failed to resolve reference "h/x:1": failed to authorize: '
                'failed to fetch anonymous token: unexpected status from GET request to h: 401', "Error: ErrImagePull",
                'Back-off pulling image "h/x:1"', "Error: ImagePullBackOff"]
        self.assertEqual(direct._why(msgs), "JFrog answered 401: no credential for this pod")
