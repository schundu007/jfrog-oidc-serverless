"""Pins the runbook counts the traceability checklist names, so nothing is dropped silently."""

import json
import unittest

from kcp_console import knowledge as k


class Counts(unittest.TestCase):
    def test_overview(self):
        self.assertEqual(len(k.STATUS_AT_A_GLANCE), 6)
        self.assertIn("imagePullSecrets: []", k.STORY)
        self.assertIn("simpler", k.DIAGRAMS["option_b_flow"]["why_b"])
        self.assertEqual(len(k.SOURCES), 4)
        self.assertIn("1.4.0 tag", k.SOURCES_CAVEAT)

    def test_environment_protected_and_case(self):
        items = {r["item"]: r for r in k.ENVIRONMENT}
        self.assertTrue(items["Another OIDC provider you will see"].get("protected"))
        self.assertIn("case-insensitively", items["Nodes carrying the provider"]["note"])
        self.assertEqual(k.PROTECTED_PROVIDERS[0]["name"], "aks-other-cluster-oidc")

    def test_pilot_scenarios(self):
        self.assertEqual(len(k.PILOT_SCENARIOS), 11)
        cfg = k.scenario("CHECK_JFROG_POD_CONFIG")
        self.assertTrue(cfg["mutates"] and cfg["restarts_daemonset"])
        self.assertEqual(cfg["changes_state"], "Yes (DaemonSet restart)")
        read_only = {s["name"] for s in k.PILOT_SCENARIOS if s["changes_state"] == "No"}
        self.assertEqual(read_only, {"FIND_CREDENTIAL_PROVIDER_WRITERS", "CHECK_AKS_NODEPOOL_CONFIG", "CHECK_NODE_VMSS_EXTENSIONS"})
        self.assertTrue(k.scenario("RESET_NODE_JFROG_ENTRY")["dry_run"])
        self.assertTrue(k.scenario("MIRROR_TEST_IMAGE")["superseded"])

    def test_phases_and_bugs(self):
        self.assertEqual([p["id"] for p in k.PHASES], ["0", "1", "2", "3", "3b", "4", "5"])
        p3b = next(p for p in k.PHASES if p["id"] == "3b")
        self.assertEqual(len(p3b["steps"]), 4)
        self.assertEqual(len(p3b["dont"]), 3)
        p4 = next(p for p in k.PHASES if p["id"] == "4")
        self.assertEqual(len(p4["values_history"]["bugs"]), 2)
        self.assertEqual(len(p4["upstream_check"]["keys"]), 5)
        self.assertEqual(len(p4["binary_modes"]["modes"]), 3)
        self.assertEqual(len(k.BUGS), 19)
        self.assertEqual(len(k.NODE_MIGRATION["steps"]), 5)
        self.assertFalse(k.NODE_MIGRATION["rollback"]["tested"])

    def test_logs_and_troubleshooting(self):
        self.assertEqual(len(k.PLUGIN_LOG_TABLE), 5)
        self.assertEqual(len(k.KUBELET_LINES), 2)
        self.assertEqual(len(k.TROUBLESHOOTING), 9)
        not_observed = {t["id"] for t in k.TROUBLESHOOTING if not t["observed"]}
        self.assertEqual(not_observed, {"token-endpoint-refused", "pull-403"})

    def test_template(self):
        self.assertEqual(len({p["path"] for p in k.TEMPLATE_PATHS}), 8)   # 7 rows; the steps row names two files
        self.assertEqual(len(k.TEMPLATE_ACTIONS), 5)
        self.assertEqual(len(k.FIRST_RUN), 5)
        self.assertEqual(len(k.MAPPING_RULES["rules"]), 5)
        self.assertEqual(len(k.LARGE_CLUSTERS), 5)
        self.assertEqual(len(k.TEMPLATE_GAPS), 6)
        names = {i["name"] for i in k.TEMPLATE_INPUTS}
        for n in ("ClusterName", "SubscriptionId", "Environment", "MatchImages", "BinaryMirrorRepo", "ArtifactoryHost",
                  "OidcProviderName", "DefaultCacheDuration", "MaxUnavailable", "Nodepools", "VerifySampleNodes",
                  "TestImage", "ResetStaleNodeEntries", "ChartVersion", "BinaryVersion"):
            self.assertIn(n, names)
        self.assertNotIn("JfrogDownloadToken", names)

    def test_security_demo_rollback_status(self):
        self.assertEqual(len(k.SECURITY_REVIEW["rows"]), 8)
        self.assertEqual({r["id"] for r in k.SECURITY_REVIEW["rows"] if r["prd_gate"]}, {"aad-preference", "sourcing"})
        self.assertEqual(len(k.DEMO["rehearsal"]), 5)
        self.assertEqual(len(k.DEMO["run_of_show"]), 7)
        self.assertTrue(k.DEMO["run_of_show"][6]["not_yet_tried"])
        self.assertEqual(len(k.DEMO["faq"]), 6)
        self.assertEqual(len(k.ROLLBACK), 5)
        self.assertEqual(len(k.OPEN_ITEMS), 8)
        self.assertEqual(len(k.TESTING_PERFORMED), 5)
        self.assertEqual(len(k.TESTING_NOT_PERFORMED), 6)

    def test_serialisable(self):
        json.dumps(k.as_dict())


if __name__ == "__main__":
    unittest.main()
