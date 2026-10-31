"""M3–M6: read-only enforcement, ADO parameters, run guards and approvals, the
checks, the report, the server in replay, fixture currency and redaction of
every stored path."""

import http.client
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from kcp_console import model, redact, report
from kcp_console.ado import AdoError, param_args
from kcp_console.checks import artifactory as af, hygiene, verify as vchecks, writers
from kcp_console.k8s import ReadOnlyError, check_helm, check_kubectl
from kcp_console.runner import RunError, build_request
from kcp_console.server import KcpServer
from kcp_console.store import replay_store
from kcp_console.tests.mount import TOKEN, Mounted, path as k8s
from kcp_console.tests.fixtures import make_fixtures

FIXTURES = Path(__file__).parent / "fixtures" / "sandbox"
BEARER = "replay-mode-ado-bearer"


class ReadOnlyTest(unittest.TestCase):
    def test_kubectl_reads_only(self):
        for ok in (["get", "nodes", "-o", "json"], ["auth", "can-i", "get", "nodes"], ["version", "-o", "json"]):
            check_kubectl(ok)
        for bad in (["delete", "pod", "x"], ["apply", "-f", "x"], ["label", "nodes", "--all", "a-"], ["exec", "p", "--", "sh"],
                    ["rollout", "restart", "ds/x"], ["auth", "reconcile"], []):
            with self.assertRaises(ReadOnlyError, msg=bad):
                check_kubectl(bad)

    def test_helm_reads_only(self):
        for ok in (["get", "values", "r", "-a"], ["list", "-A"], ["history", "r"], ["status", "r"]):
            check_helm(ok)
        for bad in (["install", "r", "c"], ["upgrade", "r", "c"], ["uninstall", "r"], ["rollback", "r", "1"], []):
            with self.assertRaises(ReadOnlyError, msg=bad):
                check_helm(bad)


class AdoParamsTest(unittest.TestCase):
    def test_booleans_lowercase(self):
        self.assertEqual(param_args({"TestScenario": "X", "DryRun": True}), ["TestScenario=X", "DryRun=true"])

    def test_secret_names_refused(self):
        for name in ("JfrogDownloadToken", "JFROG_ACCESS_TOKEN", "AdminPassword", "ApiKey", "ClientSecret"):
            with self.assertRaises(AdoError, msg=name):
                param_args({name: "x"})
        with self.assertRaises(AdoError):
            param_args({"bad name": "x"})


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.cfg = model.default_config()

    def test_dry_run_is_the_default(self):
        r = build_request(self.cfg, "pilot", "RESET_NODE_JFROG_ENTRY", {})
        self.assertIs(r["request"]["parameters"]["DryRun"], True)
        self.assertFalse(r["changes_state"])
        self.assertFalse(r["needs_typed_cluster"])
        live = build_request(self.cfg, "pilot", "RESET_NODE_JFROG_ENTRY", {"DryRun": False})
        self.assertTrue(live["changes_state"] and live["destructive"] and live["needs_typed_cluster"])
        self.assertNotEqual(r["request_sha256"], live["request_sha256"])

    def test_read_only_scenario_has_no_dry_run(self):
        r = build_request(self.cfg, "pilot", "CHECK_KUBELET_PROVIDER", {"DryRun": True})
        self.assertNotIn("DryRun", r["request"]["parameters"])
        self.assertFalse(r["changes_state"])

    def test_unknown_and_disallowed(self):
        with self.assertRaises(RunError):
            build_request(self.cfg, "pilot", "NOPE", {})
        with self.assertRaises(RunError):
            build_request(self.cfg, "pilot", "CHECK_KUBELET_PROVIDER", {"JfrogDownloadToken": "x"})
        with self.assertRaises(RunError):
            build_request(self.cfg, "jfrog", "delete-everything", {})

    def test_demo_mode_blocks_daemonset_restarts(self):
        self.cfg["demo_mode"] = True
        for name in ("INSTALL_JFROG_PROVIDER", "RESET_NODE_JFROG_ENTRY"):
            self.assertTrue(any("Demo mode" in g for g in build_request(self.cfg, "pilot", name, {})["guards"]))
        self.assertEqual(build_request(self.cfg, "pilot", "CHECK_KUBELET_PROVIDER", {})["guards"], [])

    def test_prd_gate(self):
        self.cfg["cluster"]["environment"] = "PRD"
        self.assertTrue(build_request(self.cfg, "pilot", "CHECK_KUBELET_PROVIDER", {})["guards"])
        from kcp_console import knowledge
        decided = {r["id"]: {"status": "decided"} for r in knowledge.SECURITY_REVIEW["rows"] if r.get("prd_gate")}
        self.assertEqual(build_request(self.cfg, "pilot", "CHECK_KUBELET_PROVIDER", {}, {"security": decided})["guards"], [])

    def test_chart_repo_must_be_approved(self):
        r = build_request(self.cfg, "pilot", "CHECK_JFROG_POD_CONFIG", {"ChartRepo": "https://charts.example.test"})
        self.assertTrue(any("ChartRepo" in g for g in r["guards"]))

    def test_protected_provider_is_refused(self):
        r = build_request(self.cfg, "jfrog", "delete-provider", {"provider": "aks-other-cluster-oidc"})
        self.assertTrue(any("protected" in g for g in r["guards"]))
        self.assertTrue(r["needs_typed_cluster"])

    def test_install_refuses_bad_values(self):
        self.cfg["values"]["artifactory_url"] = "https://example.jfrog.io"
        r = build_request(self.cfg, "pilot", "INSTALL_JFROG_PROVIDER", {})
        self.assertTrue(any("artifactory_url" in g for g in r["guards"]))

    def test_control_pod_defaults(self):
        p = build_request(self.cfg, "pilot", "CREATE_CONTROL_POD", {})["request"]["parameters"]
        self.assertEqual((p["TEST_POD_SA"], p["TEST_POD_NAME"]), ("default", "control-pull-test"))


class ChecksTest(unittest.TestCase):
    def test_orphaned_writer_is_critical(self):
        cfg = model.default_config()
        ds = lambda ns, name: {"metadata": {"namespace": ns, "name": name, "creationTimestamp": "2026-09-01T00:00:00Z"},
                               "spec": {"template": {"spec": {"volumes": [{"hostPath": {"path": "/var/lib/kubelet"}}],
                                                              "containers": [{"image": "jfrog/provider"}]}}}}
        found = writers.from_daemonsets({"items": [ds(cfg["k8s"]["namespace"], cfg["k8s"]["daemonset"]), ds("kube-system", "old-jfrog")]}, cfg)
        res = writers.check(found)[0]
        self.assertEqual(res["status"], "fail")
        self.assertIn("kube-system/old-jfrog", res["detail"])
        self.assertEqual(writers.check(found[:1])[0]["status"], "pass")

    def test_orphaned_mappings(self):
        m = lambda sub: {"name": sub, "claims": {"sub": sub}}
        out = af.orphaned_mappings([m("system:serviceaccount:jfrog:jfrog-provider-sa"), m("system:serviceaccount:jfrog:gone"),
                                    m("system:serviceaccount:hidden:sa"), m("nonsense")], {"jfrog": ["jfrog-provider-sa"]})
        self.assertEqual([o["status"] for o in out], ["pass", "fail", "unknown", "fail"])

    def test_binary_integrity(self):
        self.assertEqual(af.binary_integrity(0)["status"], "fail")
        self.assertEqual(af.binary_integrity(12_000_000, "a" * 64, "a" * 64)["status"], "pass")
        self.assertEqual(af.binary_integrity(12_000_000, "a" * 64, "b" * 64)["status"], "fail")

    def test_token_scope(self):
        self.assertEqual(af.token_scope(403)["status"], "info")
        self.assertEqual(af.token_scope(200)["status"], "warn")
        self.assertEqual(af.token_scope(401)["status"], "fail")

    def test_log_leaks(self):
        self.assertEqual(hygiene.log_leaks({"a": "pulled 142,944,877 bytes"})["status"], "pass")
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW0ifQ.sig"
        self.assertEqual(hygiene.log_leaks({"a": f"token {jwt}"})["status"], "fail")
        self.assertEqual(hygiene.log_leaks({"a": "Authorization: Bearer abcdefghijklmnop"})["status"], "fail")

    def test_request_log_and_attribution(self):
        text = ("2026-10-16 12:16:05,123|abc|10.0.0.4|kcp-pull-user|GET|/api/docker/aks-sandbox-test-docker-local/v2/mcp/sonarqube/manifests/1.26-v1|200|0|1|5\n"
                "2026-10-16 12:16:06,000|abd|10.0.0.4|anonymous|GET|/v2/|401|0|1|1\n"
                "garbage line\n")
        recs = vchecks.parse_request_log(text)
        self.assertEqual([r["user"] for r in recs], ["kcp-pull-user", "anonymous"])
        window = {"from": "2026-10-16T12:11:00Z", "to": "2026-10-16T12:17:00Z"}
        att = vchecks.audit_attribution(recs, "kcp-pull-user", window)
        self.assertIn(att["status"], ("pass", "PASS", "attributed"))
        self.assertEqual(vchecks.audit_attribution(recs, "kcp-pull-user", {"from": "2026-10-17T00:00:00Z", "to": "2026-10-17T00:01:00Z"})["status"] == att["status"], False)


class FixturesTest(unittest.TestCase):
    def test_fixtures_are_current(self):
        for name, text in make_fixtures.build().items():
            self.assertEqual((FIXTURES / name).read_text(), text, f"{name} is stale: python3 -m kcp_console.tests.fixtures.make_fixtures")

    def test_fixtures_hold_no_real_secret(self):
        for f in FIXTURES.iterdir():
            self.assertEqual(hygiene.log_leaks({f.name: f.read_text()})["status"], "pass", f.name)


class ReplayServerTest(unittest.TestCase):
    """The whole console in replay: the pilot story, Preflight, runs, the report, and nothing secret on disk."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = io.StringIO()
        store = replay_store(str(FIXTURES), Path(cls.tmp.name))
        cls.srv = KcpServer(TOKEN, log=cls.log, store=store, fixtures=str(FIXTURES), poll_seconds=0, sleep=lambda s: None)
        cls.mount = Mounted(cls.srv)
        cls.name = json.loads((FIXTURES / "config.seed.json").read_text())["name"]

    @classmethod
    def tearDownClass(cls):
        cls.mount.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=20)
        h = {"Host": f"127.0.0.1:{self.srv.port}", "X-Console-Token": self.srv.token}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, k8s(path), body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        ct = r.getheader("Content-Type") or ""
        return r.status, (json.loads(raw) if "json" in ct else raw.decode())

    def test_aks_list_marks_the_configured_cluster(self):
        _, r = self.call("GET", "/api/aks")
        by = {c["name"]: c for c in r["clusters"]}
        self.assertTrue(by["aks-sandbox-01"]["configured"])
        self.assertFalse(by["aks-sandbox-02"]["configured"])
        self.assertEqual(by["aks-sandbox-02"]["oidc_issuer"], "")
        self.assertEqual(by["aks-sandbox-01"]["subscription_id"], "00000000-0000-0000-0000-0000000000a1")

    def test_fleet_rows_carry_subscription_and_resource_group(self):
        _, r = self.call("GET", "/api/fleet")
        row = next(c for c in r["clusters"] if c["name"] == self.name)
        self.assertEqual((row["subscription"], row["resource_group"]), ("sandbox-subscription", "rg-aks-sandbox"))

    def test_pilot_story(self):
        s, a = self.call("GET", f"/api/nodes?config={self.name}")
        self.assertEqual(s, 200)
        self.assertEqual(a["verdict"]["status"], "INCONCLUSIVE")
        self.assertEqual(a["verdict"]["root_cause"], "cached-image")
        proof = json.dumps(a["verdict"].get("proof"))
        self.assertIn("142944877", proof.replace(",", ""))
        self.assertIn("10.487", proof)
        self.assertEqual(a["log_leaks"]["status"], "pass")
        self.assertGreaterEqual(a["checks_count"], 3)

    def test_preflight(self):
        s, out = self.call("POST", "/api/preflight", {"config": self.name})
        self.assertEqual(s, 200, out)
        self.assertNotIn("fixture_misses", out)
        checks = {c["id"]: c for c in out["preflight"]["checks"]}
        self.assertEqual(checks["provider"]["status"], "pass")
        self.assertEqual(checks["writers"]["status"], "pass")
        self.assertTrue(any(c["id"] == "foreign-providers" and "protected" in c["detail"] for c in checks.values()))

    def test_run_lifecycle_and_bearer_never_stored(self):
        s, run = self.call("POST", "/api/runs", {"config": self.name, "kind": "pilot", "name": "CHECK_KUBELET_PROVIDER", "params": {}})
        self.assertEqual(s, 200, run)
        s, err = self.call("POST", f"/api/runs/{run['id']}/approve", {"request_sha256": "0" * 64, "approved": True})
        self.assertEqual(s, 409)
        s, run = self.call("POST", f"/api/runs/{run['id']}/approve", {"request_sha256": run["request_sha256"], "approved": False})
        self.assertEqual(s, 200, run)
        self.srv.runs.wait(run["id"], timeout=20)
        s, done = self.call("GET", f"/api/runs/{run['id']}")
        self.assertEqual(done["status"], "succeeded", done)
        on_disk = "".join(p.read_text(errors="replace") for p in Path(self.tmp.name).rglob("*") if p.is_file())
        self.assertNotIn(BEARER, on_disk)
        self.assertNotIn(BEARER, self.log.getvalue())
        self.assertNotIn("replay-mode-placeholder", on_disk)

    def test_destructive_needs_typed_cluster(self):
        s, run = self.call("POST", "/api/runs", {"config": self.name, "kind": "pilot", "name": "RESET_NODE_JFROG_ENTRY",
                                                 "params": {"DryRun": False}})
        self.assertEqual(s, 200, run)
        self.assertTrue(run["needs_typed_cluster"])
        body = {"request_sha256": run["request_sha256"], "approved": True}
        self.assertEqual(self.call("POST", f"/api/runs/{run['id']}/approve", body)[0], 403)
        self.assertEqual(self.call("POST", f"/api/runs/{run['id']}/approve", {**body, "typed_cluster": "wrong"})[0], 403)
        self.assertEqual(self.call("POST", f"/api/runs/{run['id']}/cancel", {})[0], 200)

    def test_status_and_report(self):
        s, _ = self.call("PUT", f"/api/status/{self.name}", {"status": {"open_items": {"x": {"status": "done", "note": "ok"}}}})
        self.assertEqual(s, 200)
        s, st = self.call("GET", f"/api/status/{self.name}")
        self.assertEqual(st["status"]["open_items"]["x"]["status"], "done")
        s, md = self.call("GET", f"/api/report/{self.name}?format=md")
        self.assertEqual(s, 200)
        for frag in ("INCONCLUSIVE", self.name):
            self.assertIn(frag, md)
        s, js = self.call("GET", f"/api/report/{self.name}?format=json")
        self.assertEqual(s, 200)
        self.assertEqual(redact.text(json.dumps(js)), json.dumps(js))

    def test_artifactory_reads(self):
        for action in ("probe", "groups", "mappings"):
            s, out = self.call("POST", f"/api/artifactory/{action}", {"config": self.name})
            self.assertEqual(s, 200, (action, out))

    def test_status_refuses_secrets(self):
        s, _ = self.call("PUT", f"/api/status/{self.name}", {"status": {"notes": "Authorization: Bearer abcdefghijklmnopqrstu"}})
        self.assertGreaterEqual(s, 400)


class ReportTest(unittest.TestCase):
    def test_markdown_carries_the_verdict(self):
        cfg = model.default_config()
        diag = {"verdict": {"status": "PASS", "root_cause": None, "chain": [], "proof": None}, "findings": [], "three_layers": {}}
        rep = report.build(cfg, {}, diag, None, [])
        self.assertIn("PASS", report.markdown(rep))
        json.loads(report.as_json(rep))


if __name__ == "__main__":
    unittest.main()


class OneChangeAtATimeTest(unittest.TestCase):
    """Two state-changing runs on one cluster collide (the same test pod, the same helm release): a second
    approval waits until the first has finished."""

    def test_second_change_on_the_same_cluster_is_refused(self):
        from kcp_console.runner import RunManager

        class Store:
            def all(self): return []
            def save(self, run): pass

        m = RunManager(Store(), shell_factory=None)
        base = {"config": "gke-a", "changes_state": True, "needs_typed_cluster": False, "request_sha256": "h", "cluster": "gke-a"}
        m.runs = {"a": {**base, "id": "a", "status": "running"}, "b": {**base, "id": "b", "status": "prepared"}}
        with self.assertRaises(RunError) as e:
            m.approve("b", "h", approved=True)
        self.assertEqual(e.exception.status, 409)
        self.assertIn("already running", str(e.exception))
        self.assertEqual(m.runs["b"]["status"], "prepared")
