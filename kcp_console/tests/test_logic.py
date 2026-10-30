import unittest

from kcp_console import logic
from kcp_console.parse import kubelet_config
from kcp_console.tests import runbook_strings as rs


class TokenUrl(unittest.TestCase):
    def test_bare_host_gives_one_scheme(self):
        url = logic.token_url("example.jfrog.io")
        self.assertEqual(url, "https://example.jfrog.io/access/api/v1/oidc/token")
        self.assertEqual(logic.scheme_count(url), 1)

    def test_scheme_doubles_like_the_plugin(self):
        url = logic.token_url("https://example.jfrog.io")
        self.assertEqual(url, "https://https://example.jfrog.io/access/api/v1/oidc/token")
        self.assertEqual(logic.scheme_count(url), 2)
        # Go prints the doubled form as https://https//host in the error line
        self.assertEqual(logic.scheme_count("https://https//example.jfrog.io/access"), 2)

    def test_bare_host_validation(self):
        self.assertIsNone(logic.bare_host_error("example.jfrog.io"))
        self.assertIn("https://https://", logic.bare_host_error("https://example.jfrog.io"))
        self.assertIn("no path", logic.bare_host_error("example.jfrog.io/artifactory"))
        self.assertEqual(logic.bare_host_error(""), "required")


class MatchImages(unittest.TestCase):
    IMG = "example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1"

    def test_dedicated_subdomain_matches(self):
        self.assertTrue(logic.match_image("example-aks-sandbox-test-docker-local.jfrog.io", self.IMG))

    def test_wrong_registry_never_invokes_the_plugin(self):
        self.assertFalse(logic.match_image("example-jfrog-releases-remote.jfrog.io", self.IMG))
        self.assertFalse(logic.match_image("example.jfrog.io", self.IMG))

    def test_glob_is_per_label(self):
        self.assertTrue(logic.match_image("*.azurecr.io", "myreg.azurecr.io/app:1"))
        self.assertFalse(logic.match_image("*.azurecr.io", "a.b.azurecr.io/app:1"))
        self.assertFalse(logic.match_image("*.azurecr.io", self.IMG))

    def test_path_prefix_and_port(self):
        self.assertTrue(logic.match_image("reg.example.com/team", "reg.example.com/team/app:1"))
        self.assertFalse(logic.match_image("reg.example.com/team", "reg.example.com/other/app:1"))
        self.assertFalse(logic.match_image("reg.example.com:5000", "reg.example.com/app:1"))

    def test_providers_for_image(self):
        parsed = kubelet_config.parse(rs.NODE_CONFIG_FIXED)
        self.assertEqual(logic.providers_for_image(parsed["providers"], self.IMG), ["jfrog-credentials-provider"])

    def test_dedicated_host(self):
        self.assertEqual(logic.dedicated_host("example.jfrog.io", "aks-sandbox-test-docker-local"),
                         "example-aks-sandbox-test-docker-local.jfrog.io")


class MergeFiles(unittest.TestCase):
    def setUp(self):
        self.acr = kubelet_config.ACR_ENTRY
        self.new = kubelet_config.jfrog_entry("example.jfrog.io", ["h.jfrog.io"], "aks-sandbox-oidc", "5h")

    def test_fresh_node_adds(self):
        out, action = logic.merge_files([self.acr], self.new)
        self.assertEqual(action, "added")
        self.assertEqual([p["name"] for p in out], ["acr-credential-provider", "jfrog-credentials-provider"])

    def test_same_url_replaces(self):
        old = kubelet_config.jfrog_entry("example.jfrog.io", ["old.jfrog.io"], "aks-other-cluster-oidc", "5h")
        out, action = logic.merge_files([self.acr, old], self.new)
        self.assertEqual(action, "replaced")
        self.assertEqual(out[1]["matchImages"], ["h.jfrog.io"])

    def test_changed_spelling_is_not_the_same_entry(self):
        old = kubelet_config.jfrog_entry("https://example.jfrog.io", ["h.jfrog.io"], "aks-sandbox-oidc", "5h")
        out, action = logic.merge_files([self.acr, old], self.new)
        self.assertEqual(action, "kept-stale")                 # what sandbox showed: one stale entry
        self.assertEqual(logic.entry_url(out[1]), "https://example.jfrog.io")
        out, action = logic.merge_files([self.acr, old], self.new, observed=False)
        self.assertEqual(action, "appended")                   # the upstream-source reading


class ResetSafety(unittest.TestCase):
    def test_old_check_counts_env_items_and_aborts(self):
        plan = logic.reset_plan(rs.NODE_CONFIG_STALE, fixed=False)
        self.assertEqual(plan["action"], "abort")
        self.assertTrue(all("artifactory_url" in f or "azure" in f or "jfrog_oidc" in f or "disable" in f
                            for f in plan["following"]))

    def test_old_check_aborts_even_on_a_correct_node(self):
        self.assertEqual(logic.reset_plan(rs.NODE_CONFIG_FIXED, fixed=False)["action"], "abort")

    def test_fixed_check_resets_by_indentation(self):
        plan = logic.reset_plan(rs.NODE_CONFIG_STALE)
        self.assertEqual(plan["action"], "reset")
        self.assertEqual(plan["backup"], "/var/lib/kubelet/credential-provider-config.yaml.bak-before-jfrog-reset")
        after = kubelet_config.parse(plan["text"])
        self.assertEqual(after["provider_names"], ["acr-credential-provider"])

    def test_fixed_check_skips_a_correct_node(self):
        plan = logic.reset_plan(rs.NODE_CONFIG_FIXED)
        self.assertEqual(plan["action"], "skip")
        self.assertEqual(plan["reason"], "Nothing to fix on this node")

    def test_never_deletes_a_following_provider(self):
        plan = logic.reset_plan(rs.NODE_CONFIG_JFROG_FIRST)
        self.assertEqual(plan["action"], "abort")
        self.assertEqual(plan["following"], ["- name: acr-credential-provider"])

    def test_no_entry(self):
        self.assertEqual(logic.reset_plan("providers: []\n")["action"], "skip")


class Misc(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(logic.duration_seconds("5h"), 18000)
        self.assertEqual(logic.duration_seconds("30m"), 1800)
        self.assertEqual(logic.duration_seconds("1h30m"), 5400)
        self.assertIsNone(logic.duration_seconds("5 hours"))
        self.assertEqual(logic.human_duration(18000), "5h")

    def test_kubelet_older_than_config(self):
        self.assertTrue(logic.kubelet_older_than_config("2026-10-16T13:20:11Z", "2026-10-16T14:02:40Z"))
        self.assertFalse(logic.kubelet_older_than_config("2026-10-16T14:02:41Z", "2026-10-16T14:02:40Z"))
        self.assertIsNone(logic.kubelet_older_than_config("", "2026-10-16T14:02:40Z"))

    def test_realm(self):
        self.assertTrue(logic.realm_names_repo(rs.WWW_AUTH_DEDICATED, "aks-sandbox-test-docker-local"))
        self.assertFalse(logic.realm_names_repo(rs.WWW_AUTH_BARE, "aks-sandbox-test-docker-local"))

    def test_dormant(self):
        self.assertTrue(logic.is_dormant_redirect(302, rs.DORMANT_LOCATION))
        self.assertFalse(logic.is_dormant_redirect(200, ""))

    def test_download_url_arch(self):
        self.assertIsNone(logic.download_url_problem(
            "https://example.jfrog.io/artifactory/aks-sandbox-generic-local/jfrog-credential-provider/1.4.0/jfrog-credential-provider-linux"))
        self.assertIn("arch suffix", logic.download_url_problem("https://x.jfrog.io/a/jfrog-credential-provider-linux-amd64"))

    def test_binary_size(self):
        self.assertIsNone(logic.binary_size_problem(14_359_603))
        self.assertIn("under 1 MB", logic.binary_size_problem(0))
        self.assertIn("far from", logic.binary_size_problem(4_000_000))

    def test_request_hash_binds_every_part(self):
        base = logic.request_hash("argocd-utility", "feature/jfrog-kcp", {"TestScenario": "X", "DryRun": True}, "abc")
        self.assertNotEqual(base, logic.request_hash("argocd-utility", "feature/jfrog-kcp", {"TestScenario": "X", "DryRun": False}, "abc"))
        self.assertNotEqual(base, logic.request_hash("argocd-utility", "main", {"TestScenario": "X", "DryRun": True}, "abc"))
        self.assertNotEqual(base, logic.request_hash("argocd-utility", "feature/jfrog-kcp", {"TestScenario": "X", "DryRun": True}, "abd"))
        self.assertEqual(base, logic.request_hash("argocd-utility", "feature/jfrog-kcp", {"DryRun": True, "TestScenario": "X"}, "abc"))

    def test_same_node_case_insensitive(self):
        self.assertTrue(logic.same_node("aks-nodepool1-12345678-vmss00000g", "aks-nodepool1-12345678-vmss00000G"))

    def test_exchanges_per_hour(self):
        self.assertEqual(logic.exchanges_per_hour(100, 5, 2, 1800), 2000.0)


if __name__ == "__main__":
    unittest.main()
