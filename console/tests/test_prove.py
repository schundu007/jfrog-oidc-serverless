"""Prove: no stored secret, a live OIDC download, refused while the mapping is removed, restored always.
Recorded answers and a fake JFrog operator only; no cloud or JFrog access."""

import json
import tempfile
import unittest
from pathlib import Path

from console import model
from console.shell import Shell
from console.steps import prove, verify, workload_logs

workload_logs.sleep = lambda s: None

IDTOK = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
OK = {"ok": True, "claims": {"email": "svc@demo.iam.gserviceaccount.com"},
      "check": {"username": "gcp-cloudrun/t-runtime", "expires_in": 3599}, "read": {"path": "demo-generic/hello.txt", "status": 200}}
REFUSED = {"ok": False, "claims": {}, "check": {"error": "HTTP 401 from JFrog token exchange"}, "read": None}
MAPPING = {"name": "t-runtime", "description": "d", "claims": {"aud": "jfrog-gcp", "sub": "1139"},
           "token_spec": {"scope": "applied-permissions/groups:\"serverless-readers\"", "expires_in": 3600}, "priority": 1}
ENV = [{"name": "JFROG_URL", "value": "https://acme.jfrog.io"}, {"name": "JFROG_OIDC_PROVIDER", "value": "gcp-cloudrun"},
       {"name": "JFROG_HEALTHCHECK_PATH", "value": "demo-generic/hello.txt"}]


class FakeJfrog:
    def __init__(self, tokens=None, delete_status=204, by="gcp-cloudrun/t-runtime"):
        self.calls, self.mapped = [], True
        self.tokens, self.delete_status, self.by = tokens or [], delete_status, by

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/access/api/v1/tokens":
            return 200, {"tokens": self.tokens}
        if path.startswith("/artifactory/api/storage/"):
            return 200, {"lastDownloadedBy": self.by, "lastDownloaded": 1793520000000, "downloadCount": 7}
        if method == "GET" and path.endswith("/identity_mappings"):
            return 200, [MAPPING] if self.mapped else []
        if method == "DELETE":
            self.mapped = self.delete_status != 204 and self.mapped
            return self.delete_status, None
        if method == "POST":
            self.mapped = True
            return 200, body
        return 404, None


def cfg_service():
    cfg = model.default_config("gcp_run_service", name="t")
    cfg["target"]["gcp"].update(project="demo", region="us-central1", name="svc")
    return cfg


def fixtures(cfg, env=ENV, answers=(OK, REFUSED, OK)):
    describe = {"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}
    fx = [{"argv": prove.settings_argv(cfg), "rc": 0, "stdout": json.dumps(describe)},
          {"argv": verify.gcp_url_argv(cfg), "rc": 0, "stdout": "https://svc-abc.a.run.app\n"},
          {"argv": verify.GCP_ID_TOKEN, "rc": 0, "stdout": IDTOK}]
    for a in answers:
        fx.append({"http": "GET https://svc-abc.a.run.app/healthz/jfrog?fresh=1", "consume": True,
                   "status": 200 if a["ok"] else 503, "body": json.dumps(a)})
    return fx


def go(cfg, fx, jf=None, approve=None):
    lines, approvals = [], []
    jf = jf or FakeJfrog()
    sh = Shell(mode="replay", fixtures=fx, log=lines.append)
    tmp = tempfile.mkdtemp()
    p = prove.Prove(cfg, sh, lambda host: jf, approve or approvals.append, lambda rows, facts: None, Path(tmp), log=lines.append)
    prove.sleep = lambda s: None
    res = p.run()
    return res, jf, approvals, json.dumps(res) + "\n".join(lines), Path(tmp)


class ProveTests(unittest.TestCase):
    def test_full_proof_on_a_cloud_run_service(self):
        res, jf, approvals, blob, d = go(cfg_service(), fixtures(cfg_service()))
        self.assertTrue(res["proved"], res["rows"])
        self.assertEqual([r["status"] for r in res["rows"]], ["ok", "ok", "ok", "ok", "done", "ok", "done", "ok"])
        self.assertEqual(approvals[0]["mapping"], "t-runtime")
        self.assertEqual(len(approvals[0]["sha256"]), 64)
        methods = [(m, p) for m, p, _ in jf.calls if m != "GET"]
        self.assertEqual(methods, [("DELETE", "/access/api/v1/oidc/gcp-cloudrun/identity_mappings/t-runtime"),
                                   ("POST", "/access/api/v1/oidc/gcp-cloudrun/identity_mappings")])
        restored = [b for m, _, b in jf.calls if m == "POST"][0]
        self.assertEqual(restored["claims"], MAPPING["claims"])
        self.assertEqual(res["facts"]["who"]["last_downloaded_by"], "gcp-cloudrun/t-runtime")
        self.assertFalse((d / "removed-mapping.json").exists())
        self.assertNotIn(IDTOK, blob)

    def test_jfrog_answers_per_stage_and_the_workloads_live_log(self):
        cfg = cfg_service()
        fx = fixtures(cfg)
        line = {"event": "jfrog_oidc_selftest", "ok": False, "jfrog_status": 401, "provider": "gcp-cloudrun",
                "message": "JFrog refused the exchange via gcp-cloudrun: HTTP 401"}
        fx.append({"argv": workload_logs.argv(cfg, "2026-11-01T09:10:00Z"), "rc": 0,
                   "stdout": json.dumps([{"timestamp": "2026-11-01T09:10:05Z", "severity": "INFO", "jsonPayload": line}])})
        lines, approvals = [], []
        p = prove.Prove(cfg, Shell(mode="replay", fixtures=fx, log=lines.append), lambda h: FakeJfrog(), approvals.append,
                        lambda r, f: None, Path(tempfile.mkdtemp()), clock=lambda: 1793524200)
        prove.sleep = lambda s: None
        res = p.run()
        self.assertEqual([(a["stage"], a["status"]) for a in res["facts"]["answers"]],
                         [("granted", 200), ("refused", 401), ("repull", 200)])
        log = res["facts"]["workload_log"]
        self.assertEqual(log["rows"][0]["level"], "ERROR")
        self.assertEqual(log["jfrog"]["status"], 401)

    def test_stored_token_stops_before_anything_changes(self):
        env = ENV + [{"name": "JFROG_ACCESS_TOKEN", "value": "s3cr3t-value"}]
        res, jf, approvals, blob, _ = go(cfg_service(), fixtures(cfg_service(), env=env))
        self.assertFalse(res["proved"])
        self.assertEqual(res["rows"][0]["status"], "failed")
        self.assertIn("JFROG_ACCESS_TOKEN", res["rows"][0]["text"])
        self.assertEqual((approvals, [c for c in jf.calls if c[0] != "GET"]), ([], []))
        self.assertNotIn("s3cr3t-value", blob)

    def test_long_lived_token_fails_the_proof(self):
        jf = FakeJfrog(tokens=[{"subject": "jfac@x/users/gcp-cloudrun/t-runtime", "issued_at": 0, "expiry": 31536000}])
        res, jf, approvals, _, _ = go(cfg_service(), fixtures(cfg_service()), jf=jf)
        self.assertEqual(res["rows"][2]["status"], "failed")
        self.assertEqual(approvals, [])

    def test_still_reading_without_trust_fails_and_restores(self):
        res, jf, _, _, d = go(cfg_service(), fixtures(cfg_service(), answers=(OK, OK, OK)))
        self.assertFalse(res["proved"])
        st = {r["id"]: r["status"] for r in res["rows"]}
        self.assertEqual((st["refused"], st["restore"], st["repull"]), ("failed", "done", "ok"))
        self.assertTrue(jf.mapped)
        self.assertFalse((d / "removed-mapping.json").exists())

    def test_cancel_at_the_approval_changes_nothing(self):
        class Cancelled(Exception):
            pass

        def approve(info):
            raise Cancelled()
        with self.assertRaises(Cancelled):
            go(cfg_service(), fixtures(cfg_service()), approve=approve)

    def test_failed_delete_leaves_nothing_to_restore(self):
        jf = FakeJfrog(delete_status=403)
        res, jf, _, _, _ = go(cfg_service(), fixtures(cfg_service()), jf=jf)
        st = {r["id"]: r["status"] for r in res["rows"]}
        self.assertEqual(st["break"], "failed")
        self.assertNotEqual(st["restore"], "done")
        self.assertNotIn("POST", [m for m, _, _ in jf.calls])

    def test_app_settings_are_read_sensitive_and_only_names_kept(self):
        cfg = model.default_config("azure_functions_premium", name="t")
        cfg["target"]["azure"].update(subscription="s", resource_group="rg", name="fn")
        settings = [{"name": "JFROG_URL", "value": "https://acme.jfrog.io"}, {"name": "AzureWebJobsStorage", "value": "DefaultEndpoints;AccountKey=abc"},
                    {"name": "DOCKER_REGISTRY_SERVER_PASSWORD", "value": "pw-should-not-show"}]
        sh = Shell(mode="replay", fixtures=[{"argv": prove.settings_argv(cfg), "rc": 0, "stdout": json.dumps(settings)}], log=[].append)
        s = prove.read_settings(cfg, sh)
        self.assertEqual(s["values"], {"JFROG_URL": "https://acme.jfrog.io"})
        self.assertIn("DOCKER_REGISTRY_SERVER_PASSWORD", s["names"])
        self.assertNotIn("pw-should-not-show", json.dumps(s) + json.dumps(sh.recorded))
        self.assertTrue(prove.SECRET_NAME.search("DOCKER_REGISTRY_SERVER_PASSWORD"))
        for ok in ("JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_HEALTHCHECK_PATH", "JFROG_URL"):
            self.assertFalse(prove.SECRET_NAME.search(ok), ok)


if __name__ == "__main__":
    unittest.main()
