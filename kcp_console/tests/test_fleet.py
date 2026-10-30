"""The fleet: one row per cluster from stored status and run records, no live calls."""

import unittest

from kcp_console import fleet

ISSUER = "https://issuer.example/x/"


def run(name, cfg, dry, status, created, steps=None):
    return {"id": f"{cfg}-{name}-{created}", "config": cfg, "kind": "direct", "name": name, "status": status,
            "created": created, "request": {"parameters": {"DryRun": dry}}, "result": {"steps": steps or []}}


PROOF_OK = [{"id": i, "status": "ok", "detail": ""} for i in ("cluster", "sa-check", "trust", "map", "anon", "who")] + \
           [{"id": "pull", "status": "done", "detail": ""}]


class RowTest(unittest.TestCase):
    def test_working_cluster(self):
        st = {"discover": {"release": {"chart": "jfrog-credential-provider-1.4.0"}, "nodes": ["a", "b"],
                           "cluster": {"kubernetes_version": "1.36", "location": "westus2"},
                           "daemonsets": [{"jfrog": True, "ready": 2, "desired": 2}]},
              "discover_at": "2026-10-30T08:00:00Z", "preflight": {"checks": [{"status": "pass"}]}}
        runs = [run("verify", "a", False, "succeeded", "2026-10-30T09:00:00Z", PROOF_OK)]
        r = fleet.row({"name": "a", "cluster": "aks-a", "platform": "aks"}, st, runs)
        self.assertEqual((r["state"], r["oidc"], r["chart"], r["nodes"], r["ready"]), ("working", "working", "1.4.0", 2, "2/2"))
        self.assertEqual(r["cloud"], "aks")
        self.assertEqual(r["verified_at"], "2026-10-30T09:00:00Z")

    def test_states(self):
        self.assertEqual(fleet.row({"name": "n"}, {}, [])["state"], "unread")
        self.assertEqual(fleet.row({"name": "n"}, {"discover": {}}, [])["state"], "not_installed")
        bad = {"discover": {"release": {"chart": "x-1"}}, "preflight": {"checks": [{"status": "fail", "title": "OIDC issuer off"}]}}
        r = fleet.row({"name": "n"}, bad, [])
        self.assertEqual((r["state"], r["problem"]), ("attention", "OIDC issuer off"))
        inst = {"discover": {"release": {"chart": "x-1"}}}
        self.assertEqual(fleet.row({"name": "n"}, inst, [])["state"], "unverified")
        failed = [run("verify", "n", False, "failed", "2026-10-30T09:00:00Z", [{"id": "pull", "status": "done"}, {"id": "who", "status": "failed"}])]
        self.assertEqual(fleet.row({"name": "n"}, inst, failed)["state"], "not_proven")

    def test_pending_approval_wins(self):
        inst = {"discover": {"release": {"chart": "x-1"}}}
        runs = [run("setup", "n", False, "prepared", "2026-10-30T10:00:00Z"),
                run("setup", "n", True, "succeeded", "2026-10-30T09:59:00Z")]
        r = fleet.row({"name": "n"}, inst, runs)
        self.assertEqual((r["state"], r["action"]["which"], r["action"]["status"]), ("approval", "setup", "prepared"))

    def test_summary_counts_and_other_configs_runs_ignored(self):
        runs = [run("verify", "b", False, "succeeded", "2026-10-30T09:00:00Z", PROOF_OK)]
        rows = fleet.rows([{"name": "a"}, {"name": "b"}], {"a": {}, "b": {"discover": {"release": {"chart": "c-1"}}}}, runs)
        self.assertEqual([r["state"] for r in rows], ["working", "unread"])  # urgency order
        self.assertEqual(fleet.counts(rows), {"all": 2, "working": 1, "unread": 1})


if __name__ == "__main__":
    unittest.main()
