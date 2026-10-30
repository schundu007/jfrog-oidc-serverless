"""M1: the model, the simulator and the rule engine."""

import copy
import unittest

from kcp_console import diagnose, model
from kcp_console.sim import engine
from kcp_console.tests import runbook_strings as rs


class ModelTest(unittest.TestCase):
    def test_default_values_render_the_pilot_file(self):
        self.assertEqual(model.render_values(model.default_config()), rs.VALUES_SANDBOX)

    def test_defaults_validate(self):
        self.assertEqual(model.errors(model.validate(model.default_config())), [])

    def test_scheme_in_artifactory_url_is_an_error(self):
        cfg = model.default_config()
        cfg["values"]["artifactory_url"] = "https://example.jfrog.io"
        self.assertTrue(model.errors(model.validate(cfg)))

    def test_arch_suffix_in_download_url_is_an_error(self):
        cfg = model.default_config()
        cfg["values"]["download_url"] += "-amd64"
        self.assertTrue(model.errors(model.validate(cfg)))

    def test_protected_provider_is_refused(self):
        cfg = model.default_config()
        cfg["values"]["provider_name"] = "aks-other-cluster-oidc"
        self.assertTrue(model.errors(model.validate(cfg)))


class ScenarioTest(unittest.TestCase):
    def test_there_are_23_scenarios(self):
        self.assertEqual(len(engine.names()), 23)

    def test_every_scenario_matches_its_expectations(self):
        for name in engine.names():
            with self.subTest(name):
                self.assertEqual(engine.check(engine.load(name)), [])

    def test_unknown_op_is_refused(self):
        with self.assertRaises(ValueError):
            engine.run({"name": "x", "steps": [["__init__"]]})

    def test_unknown_scenario_and_path_escape(self):
        for bad in ("nope", "../world"):
            with self.assertRaises(KeyError):
                engine.load(bad)

    def test_runs_are_deterministic(self):
        s = engine.load("stale-node-entry-after-fix")
        a, b = engine.run(s), engine.run(s)
        self.assertEqual(a["chain"], b["chain"])
        self.assertEqual(a["events"], b["events"])

    def test_findings_name_a_dry_run_first_action(self):
        r = engine.run(engine.load("stale-node-entry-after-fix"))
        top = r["diagnosis"]["findings"][0]
        self.assertEqual(top["next"], {"scenario": "RESET_NODE_JFROG_ENTRY", "dry_run": True})
        self.assertTrue(top["stale"])

    def test_three_layer_diff_flags_the_node_layer(self):
        r = engine.run(engine.load("stale-node-entry-after-fix"))
        layers = {p["layer"] for n in r["diagnosis"]["three_layers"]["nodes"] for p in n["problems"]}
        self.assertIn("node", layers)
        self.assertEqual(engine.run(engine.load("happy-path"))["diagnosis"]["three_layers"]["status"], "pass")

    def test_not_observed_rules_are_labelled(self):
        r = engine.run(engine.load("mapping-sub-mismatch"))
        f = next(f for f in r["diagnosis"]["findings"] if f["rule"] == "token-endpoint-refused")
        self.assertFalse(f["observed"])
        self.assertTrue(f["label"])

    def test_bundle_holds_no_token_material(self):
        for name in engine.names():
            text = repr(engine.run(engine.load(name))["bundle"])
            self.assertNotRegex(text, r"eyJ[A-Za-z0-9_-]{10,}")
            self.assertNotIn("accessToken=", text.replace("accessToken=***", ""))


class DiagnoseRunbookTest(unittest.TestCase):
    """The rule engine on the runbook's own strings, not simulator output."""

    def bundle(self, **kw):
        b = {"config": model.default_config(), "values_file": rs.VALUES_SANDBOX, "pods": [], "kubelet_checks": []}
        b.update(kw)
        return b

    def pod(self, events, node="aks-nodepool1-12345678-vmss00000g", time="2026-10-16T13:40:05Z"):
        return {"name": "secretless-pull-test", "image": model.default_config()["jfrog"]["test_image"],
                "sa": "jfrog-provider-sa", "node": node, "kind": "test", "events": events, "time": time}

    def test_before_fix(self):
        d = diagnose.diagnose(self.bundle(pods=[self.pod(rs.EVENTS_FAIL)], kubelet_checks=[rs.CHECK_KUBELET_BEFORE]))
        self.assertEqual(d["verdict"]["status"], "FAIL")
        self.assertEqual(d["verdict"]["root_cause"], "doubled-https")

    def test_after_fix(self):
        d = diagnose.diagnose(self.bundle(pods=[self.pod(rs.EVENTS_PASS, time="2026-10-16T14:03:16Z")],
                                          kubelet_checks=[rs.CHECK_KUBELET_AFTER]))
        self.assertEqual(d["verdict"]["status"], "PASS")
        self.assertTrue(d["verdict"]["pod"]["exchange"].startswith("fresh"))

    def test_not_restarted(self):
        d = diagnose.diagnose(self.bundle(pods=[self.pod(rs.EVENTS_FAIL)], kubelet_checks=[rs.CHECK_KUBELET_NOT_RESTARTED]))
        self.assertEqual(d["verdict"]["root_cause"], "kubelet-old-config")

    def test_cached_image_is_inconclusive(self):
        d = diagnose.diagnose(self.bundle(pods=[self.pod(rs.EVENTS_CACHED)]))
        self.assertEqual(d["verdict"]["status"], "INCONCLUSIVE")

    def test_forbidden_user_principal(self):
        p = self.pod(rs.EVENTS_FAIL + "\n" + rs.FORBIDDEN_USER)
        rules = {f["rule"] for f in diagnose.diagnose(self.bundle(pods=[p]))["findings"]}
        self.assertIn("forbidden-user-principal", rules)

    def test_truncated_output_falls_back_to_full_log(self):
        cut = rs.CHECK_KUBELET_BEFORE.split("QUICK SUMMARY")[0]
        d = diagnose.diagnose(self.bundle(pods=[self.pod(rs.EVENTS_FAIL)], kubelet_checks=[cut]))
        self.assertIn("output-truncated", {f["rule"] for f in d["findings"]})

    def test_audit_attribution(self):
        user = model.default_config()["jfrog"]["mapped_user"]
        rec = {"service": "artifactory", "path": "/api/docker/r/v2/mcp/sonarqube/manifests/1.26-v1", "status": 200}
        self.assertEqual(diagnose.attribution([dict(rec, user=user)], user)["status"], "PASS")
        self.assertEqual(diagnose.attribution([dict(rec, user="static-svc")], user)["status"], "FAIL")
        self.assertEqual(diagnose.attribution([], user)["status"], "NOT RUN")


if __name__ == "__main__":
    unittest.main()
