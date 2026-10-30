import unittest

from kcp_console.parse import events, helm, kubelet_config, plugin_log, summary, yamlish
from kcp_console.tests import runbook_strings as rs


class Yamlish(unittest.TestCase):
    def test_values_file(self):
        doc = yamlish.load(rs.VALUES_SANDBOX)
        pc = doc["providerConfig"][0]
        self.assertEqual(pc["artifactoryUrl"], "example.jfrog.io")
        self.assertEqual(pc["matchImages"], ["example-aks-sandbox-test-docker-local.jfrog.io"])
        self.assertEqual(pc["azure"]["azure_app_audience"], "api://AzureADTokenExchange")
        self.assertIs(pc["tokenAttributes"]["enabled"], True)
        self.assertEqual(doc["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
                         ["nodeSelectorTerms"][0]["matchExpressions"][0]["values"], ["true"])

    def test_flow_lists_and_block_scalars(self):
        doc = yamlish.load('a: ["*.azurecr.io", b]\nb: |\n  line1\n  line2\nc: {}\nd: []\n')
        self.assertEqual(doc["a"], ["*.azurecr.io", "b"])
        self.assertEqual(doc["b"], "line1\nline2\n")
        self.assertEqual(doc["c"], {})
        self.assertEqual(doc["d"], [])

    def test_load_all(self):
        docs = yamlish.load_all(rs.HELM_MANIFEST)
        self.assertEqual([d["kind"] for d in docs], ["ConfigMap", "Role"])


class KubeletConfig(unittest.TestCase):
    def test_stale_entry(self):
        p = kubelet_config.parse(rs.NODE_CONFIG_STALE)
        self.assertEqual(p["provider_names"], ["acr-credential-provider", "jfrog-credentials-provider"])
        self.assertEqual(p["jfrog_entries"], 1)
        s = kubelet_config.jfrog_summary(p)
        self.assertEqual(s["artifactory_url"], "https://example.jfrog.io")
        self.assertEqual(s["match_images"], ["example-aks-sandbox-test-docker-local.jfrog.io"])
        self.assertEqual(s["provider_name"], "aks-sandbox-oidc")
        self.assertEqual(s["disable_provider_autoupdate"], "true")

    def test_chart_supplied_token_attributes_present(self):
        self.assertEqual(kubelet_config.missing_chart_supplied(kubelet_config.parse(rs.NODE_CONFIG_FIXED)), [])

    def test_render_roundtrip(self):
        entry = kubelet_config.jfrog_entry("example.jfrog.io", ["h.jfrog.io"], "aks-sandbox-oidc", "5h")
        text = kubelet_config.render([kubelet_config.ACR_ENTRY, entry])
        p = kubelet_config.parse(text)
        self.assertEqual(kubelet_config.jfrog_summary(p)["artifactory_url"], "example.jfrog.io")
        self.assertEqual(p["providers"][0]["matchImages"][0], "*.azurecr.io")
        self.assertEqual(kubelet_config.missing_chart_supplied(p), [])


class PluginLog(unittest.TestCase):
    def test_table_kinds(self):
        entries = plugin_log.parse(rs.PLUGIN_LOG)
        kinds = [e["kind"] for e in entries]
        for k in ("envs", "sa_token", "token_url", "dns_error", "autoupdate"):
            self.assertIn(k, kinds)
        envs = next(e for e in entries if e["kind"] == "envs")
        self.assertEqual(envs["provider"], "aks-sandbox-oidc")

    def test_newest_url_wins(self):
        s = plugin_log.summary(plugin_log.parse(rs.PLUGIN_LOG))
        self.assertEqual(s["newest_schemes"], 1)
        self.assertTrue(s["doubled_seen"])
        self.assertEqual(s["dns_errors"], 1)

    def test_plain_text_lines(self):
        s = plugin_log.summary(plugin_log.parse(rs.PLUGIN_DOUBLED + "\n" + rs.PLUGIN_DNS_ERROR))
        self.assertEqual(s["newest_schemes"], 2)

    def test_readonly_artifact(self):
        e = plugin_log.parse_line("open /var/log/jfrog-credentials-provider/x.log: read-only file system")
        self.assertEqual(e["kind"], "readonly_fs")
        self.assertIn("Diagnostic artifact", e["meaning"])


class Events(unittest.TestCase):
    def test_anonymous_401(self):
        ev = events.parse_pod_events(rs.EVENTS_FAIL)
        self.assertTrue(ev["backoff"])
        v = events.pull_verdict(ev)
        self.assertEqual(v["status"], "FAIL")
        self.assertIn("not invoked", v["detail"])

    def test_successful_pull(self):
        ev = events.parse_pod_events(rs.EVENTS_PASS)
        self.assertEqual(ev["pulled"]["bytes"], 142944877)
        self.assertEqual(ev["pulled"]["seconds"], 10.487)
        self.assertEqual(events.pull_verdict(ev)["status"], "PASS")

    def test_cached_is_inconclusive(self):
        self.assertEqual(events.pull_verdict(events.parse_pod_events(rs.EVENTS_CACHED))["status"], "INCONCLUSIVE")

    def test_forbidden_user_principal(self):
        ev = events.parse_pod_events(rs.FORBIDDEN_USER + "\n" + rs.FORBIDDEN_SP)
        self.assertEqual(ev["forbidden"][0]["user_principal"], "operator@example.com")
        self.assertTrue(ev["forbidden"][0]["azure_rbac"])
        self.assertIsNone(ev["forbidden"][1]["user_principal"])

    def test_journal(self):
        j = events.parse_journal(rs.JOURNAL_EXEC_FAILED)
        self.assertEqual(j["exec_failed"][0]["plugin"], "jfrog-credentials-provider")
        self.assertEqual(j["exec_failed"][0]["rc"], 1)


class Helm(unittest.TestCase):
    def test_values_summary(self):
        s = helm.values_summary(rs.VALUES_SANDBOX)
        self.assertEqual(s["artifactory_url"], "example.jfrog.io")
        self.assertEqual(s["provider_name"], "aks-sandbox-oidc")
        self.assertEqual(s["default_cache_duration"], "5h")
        self.assertEqual(s["node_affinity_label"], "credentialsProviderEnabled")

    def test_manifest_summary(self):
        s = helm.manifest_summary(rs.HELM_MANIFEST)
        self.assertEqual(s["artifactory_url"], "example.jfrog.io")
        self.assertEqual(s["provider_name"], "aks-sandbox-oidc")
        self.assertEqual(s["match_images"], ["example-aks-sandbox-test-docker-local.jfrog.io"])
        self.assertEqual(s["chart"], "jfrog-credential-provider-1.4.0")

    def test_rbac(self):
        objs = helm.rbac_objects(rs.HELM_MANIFEST)
        self.assertEqual(objs[0]["rules"][0]["verbs"], ["get", "list", "watch"])
        self.assertEqual(helm.broad_rbac(objs), [])

    def test_release_meta(self):
        self.assertEqual(helm.release_meta(rs.CHECK_POD_CONFIG)["chart_version"], "1.4.0")


class QuickSummary(unittest.TestCase):
    def test_footer_before_fix(self):
        q = summary.quick_summary(rs.CHECK_KUBELET_BEFORE)
        self.assertEqual(q["source"], "summary")
        n = q["nodes"][0]
        self.assertEqual(n["node"], "aks-nodepool1-12345678-vmss00000g")
        self.assertEqual(n["entry"]["artifactory_url"], "https://example.jfrog.io")
        self.assertEqual(n["entry"]["entries"], 1)
        self.assertEqual(n["plugin"]["newest_schemes"], 2)
        self.assertEqual(n["binary_bytes"], 14359603)
        self.assertTrue(n["flags_ok"])

    def test_footer_after_fix_goes_by_newest_line(self):
        n = summary.quick_summary(rs.CHECK_KUBELET_AFTER)["nodes"][0]
        self.assertEqual(n["entry"]["artifactory_url"], "example.jfrog.io")
        self.assertEqual(n["plugin"]["newest_schemes"], 1)
        self.assertFalse(n["kubelet_older_than_config"])

    def test_kubelet_not_restarted(self):
        n = summary.quick_summary(rs.CHECK_KUBELET_NOT_RESTARTED)["nodes"][0]
        self.assertTrue(n["kubelet_older_than_config"])

    def test_truncated_log_falls_back(self):
        cut = rs.CHECK_KUBELET_BEFORE.split("QUICK SUMMARY")[0]
        q = summary.quick_summary(cut)
        self.assertEqual(q["source"], "full-log")
        self.assertEqual(q["nodes"][0]["plugin"]["newest_schemes"], 2)

    def test_pod_config_sections(self):
        s = summary.pod_config_sections(rs.CHECK_POD_CONFIG)
        self.assertEqual(s["jfrog_exchange_annotation"], "true")
        self.assertTrue(s["restarted_daemonset"])
        self.assertEqual(helm.values_summary(s["helm_values"])["artifactory_url"], "example.jfrog.io")
        self.assertEqual(helm.manifest_summary(s["rendered"])["artifactory_url"], "example.jfrog.io")


if __name__ == "__main__":
    unittest.main()
