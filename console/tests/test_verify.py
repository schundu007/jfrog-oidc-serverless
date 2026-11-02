"""Verify: read-only live proof that each workload exchanges its own identity for a JFrog token.
Recorded answers only; no cloud access. Never a token or key in the result or the log."""

import json
import unittest

from console import model
from console.shell import Shell
from console.steps import verify

SUB = "11111111-2222-3333-4444-555555555555"
SECRET = "k3y-SHOULD-NEVER-SHOW-0123456789abcdef"
IDTOK = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"

SELFTEST_OK = {"ok": True, "claims": {"email": "svc@demo.iam.gserviceaccount.com", "sub": "1139", "aud": "jfrog-gcp",
                                      "iss": "https://accounts.google.com"},
               "check": {"username": "gcp-cloudrun/svc-runtime", "expires_in": 3599, "provider": "gcp-cloudrun", "source": "gcp"},
               "read": {"path": "demo-generic/hello.txt", "status": 200}}
SELFTEST_FAIL = {"ok": False, "claims": {"sub": "1139"}, "check": {"error": "401 Unauthorized: no mapping matched"}, "read": None}


def gcp(kind, name):
    cfg = model.default_config(kind, name="t")
    cfg["target"]["gcp"].update(project="demo", region="us-central1", name=name)
    return cfg


def azure(kind, name, rg="rg-x"):
    cfg = model.default_config(kind, name="t")
    cfg["target"]["azure"].update(subscription=SUB, resource_group=rg, name=name)
    return cfg


def cmd(argv, stdout="", rc=0, stderr=""):
    return {"argv": [argv[0], *argv[1:]], "rc": rc, "stdout": stdout, "stderr": stderr}


def go(cfg, fixtures):
    lines = []
    sh = Shell(mode="replay", fixtures=fixtures, log=lines.append)
    res = verify.run(cfg, sh)
    blob = json.dumps(res) + "\n".join(lines)
    return res, sh, blob


class HealthEndpointTests(unittest.TestCase):
    def test_cloud_run_service_pass(self):
        cfg = gcp("gcp_run_service", "svc")
        res, sh, blob = go(cfg, [
            cmd(verify.gcp_url_argv(cfg), "https://svc-abc.a.run.app\n"),
            cmd(verify.GCP_ID_TOKEN, IDTOK + "\n"),
            {"http": "GET https://svc-abc.a.run.app/healthz/jfrog", "status": 200, "body": json.dumps(SELFTEST_OK)},
        ])
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["status"], "pass")
        self.assertEqual(res["jfrog_user"], "gcp-cloudrun/svc-runtime")
        self.assertEqual(res["identity"], "svc@demo.iam.gserviceaccount.com")
        self.assertEqual(res["read"], "demo-generic/hello.txt: 200")
        self.assertNotIn(IDTOK, blob)

    def test_cloud_run_function_uses_same_endpoint(self):
        cfg = gcp("gcp_run_function", "fn")
        res, sh, _ = go(cfg, [
            cmd(verify.gcp_url_argv(cfg), "https://fn-abc.a.run.app\n"),
            cmd(verify.GCP_ID_TOKEN, IDTOK),
            {"http": "GET https://fn-abc.a.run.app/healthz/jfrog", "status": 200, "body": json.dumps(SELFTEST_OK)},
        ])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))

    def test_exchange_refused_fails_with_reason(self):
        cfg = gcp("gcp_run_service", "svc")
        res, _, _ = go(cfg, [
            cmd(verify.gcp_url_argv(cfg), "https://svc-abc.a.run.app"),
            cmd(verify.GCP_ID_TOKEN, IDTOK),
            {"http": "GET https://svc-abc.a.run.app/healthz/jfrog", "status": 503, "body": json.dumps(SELFTEST_FAIL)},
        ])
        self.assertEqual(res["status"], "fail")
        self.assertIn("no mapping matched", res["reason"])

    def test_container_app_no_auth(self):
        cfg = azure("azure_container_app", "aca")
        res, sh, _ = go(cfg, [
            cmd(verify.aca_fqdn_argv(cfg), "aca.example.azurecontainerapps.io\n"),
            {"http": "GET https://aca.example.azurecontainerapps.io/healthz/jfrog", "status": 200, "body": json.dumps(SELFTEST_OK)},
        ])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))

    def test_function_app_key_never_shown(self):
        for kind in ("azure_functions_premium", "azure_functions_flex"):
            cfg = azure(kind, "func-x")
            res, sh, blob = go(cfg, [
                cmd(verify.site_host_argv(cfg), "func-x.azurewebsites.net\n"),
                cmd(verify.site_key_argv(cfg), SECRET + "\n"),
                {"http": "GET https://func-x.azurewebsites.net/api/healthz/jfrog", "status": 200, "body": json.dumps(SELFTEST_OK)},
            ])
            self.assertEqual((sh.misses, res["status"]), ([], "pass"), kind)
            self.assertNotIn(SECRET, blob)

    def test_functions_on_aca_key_api_failure_is_a_clear_fail(self):
        cfg = azure("azure_functions_aca", "func-aca")
        res, _, blob = go(cfg, [
            cmd(verify.aca_fqdn_argv(cfg), "func-aca.example.io"),
            cmd(verify.aca_revision_argv(cfg), "func-aca--0000001"),
            cmd(verify.aca_keys_argv(cfg, "func-aca--0000001"),
                json.dumps({"error": {"code": "UnexpectedException", "message": "Failed to get or create ephemeral container."}})),
        ])
        self.assertEqual(res["status"], "fail")
        self.assertIn("function keys", res["reason"])
        self.assertIn("ephemeral container", res["reason"])

    def test_functions_on_aca_scaled_to_zero_is_woken(self):
        cfg = azure("azure_functions_aca", "func-aca")
        zero = json.dumps({"error": {"message": "Replicas set to 0. Ensure that there is at least one replica."}})
        verify.sleep, saved = (lambda s: None), verify.sleep
        self.addCleanup(setattr, verify, "sleep", saved)
        res, sh, blob = go(cfg, [
            cmd(verify.aca_fqdn_argv(cfg), "func-aca.example.io"),
            cmd(verify.aca_revision_argv(cfg), "func-aca--0000001"),
            {**cmd(verify.aca_keys_argv(cfg, "func-aca--0000001"), zero, rc=1), "consume": True},
            {"http": "GET https://func-aca.example.io/", "status": 404, "body": ""},
            cmd(verify.aca_keys_argv(cfg, "func-aca--0000001"), json.dumps({"keys": [{"name": "default", "value": SECRET}]})),
            {"http": "GET https://func-aca.example.io/api/healthz/jfrog", "status": 200, "body": json.dumps(SELFTEST_OK)},
        ])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))
        self.assertNotIn(SECRET, blob)

    def test_functions_on_aca_pass(self):
        cfg = azure("azure_functions_aca", "func-aca")
        res, sh, blob = go(cfg, [
            cmd(verify.aca_fqdn_argv(cfg), "func-aca.example.io"),
            cmd(verify.aca_revision_argv(cfg), "func-aca--0000001"),
            cmd(verify.aca_keys_argv(cfg, "func-aca--0000001"), json.dumps({"keys": [{"name": "default", "value": SECRET}]})),
            {"http": "GET https://func-aca.example.io/api/healthz/jfrog", "status": 200, "body": json.dumps(SELFTEST_OK)},
        ])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))
        self.assertNotIn(SECRET, blob)


class LogTests(unittest.TestCase):
    def test_worker_pool_latest_check(self):
        cfg = gcp("gcp_run_worker_pool", "wp")
        entry = {"timestamp": "2026-11-01T08:18:08Z", "jsonPayload": {"event": "jfrog_check", "ok": True,
                 "claims": {"email": "wp@demo.iam.gserviceaccount.com"}, "check": {"username": "gcp-cloudrun/wp-runtime", "expires_in": 2699}}}
        res, sh, _ = go(cfg, [cmd(verify.worker_pool_logs_argv(cfg), json.dumps([entry]))])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))
        self.assertEqual(res["jfrog_user"], "gcp-cloudrun/wp-runtime")
        self.assertEqual(res["when"], "2026-11-01T08:18:08Z")

    def test_worker_pool_no_check_logged(self):
        cfg = gcp("gcp_run_worker_pool", "wp")
        res, _, _ = go(cfg, [cmd(verify.worker_pool_logs_argv(cfg), "[]")])
        self.assertEqual(res["status"], "fail")

    def test_cloud_run_job_last_execution(self):
        cfg = gcp("gcp_run_job", "job")
        ex = [{"metadata": {"name": "job-7n5pz"}, "status": {"succeededCount": 1, "completionTime": "2026-11-01T08:10:08Z"},
               "spec": {"template": {"spec": {"serviceAccountName": "job@demo.iam.gserviceaccount.com"}}}}]
        logs = [{"textPayload": "read demo-generic/hello.txt: Hello"},
                {"textPayload": "exchanged identity for a JFrog token valid 3599s"}]
        res, sh, _ = go(cfg, [cmd(verify.gcp_executions_argv(cfg), json.dumps(ex)),
                              cmd(verify.gcp_job_logs_argv(cfg, "job-7n5pz"), json.dumps(logs))])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))
        self.assertEqual(res["identity"], "job@demo.iam.gserviceaccount.com")
        self.assertEqual(res["read"], "demo-generic/hello.txt")
        self.assertNotIn("Hello", json.dumps(res))   # the artifact's content is not evidence

    def test_cloud_run_job_never_ran(self):
        cfg = gcp("gcp_run_job", "job")
        res, _, _ = go(cfg, [cmd(verify.gcp_executions_argv(cfg), "[]")])
        self.assertEqual(res["status"], "fail")
        self.assertIn("Build and deploy", res["reason"])

    def test_container_apps_job(self):
        cfg = azure("azure_container_app_job", "caj", rg="rg-reader")
        env = "/subscriptions/x/resourceGroups/rg-reader/providers/Microsoft.App/managedEnvironments/cae"
        ex = [{"name": "caj-old", "properties": {"status": "Succeeded", "startTime": "2026-10-01T00:00:00Z"}},
              {"name": "caj-new", "properties": {"status": "Succeeded", "startTime": "2026-11-01T08:09:00Z"}}]
        logs = [{"Log_s": "exchanged identity for a JFrog token valid 3599s"}, {"Log_s": "read demo-generic/hello.txt: Hello"}]
        res, sh, _ = go(cfg, [
            cmd(verify.caj_executions_argv(cfg), json.dumps(ex)),
            cmd(verify.caj_env_argv(cfg), env + "\n"),
            cmd(verify.workspace_argv(env), "ws-123\n"),
            cmd(verify.caj_logs_argv("ws-123", "caj-new"), json.dumps(logs)),
        ])
        self.assertEqual((sh.misses, res["status"]), ([], "pass"))
        self.assertEqual(res["where"], "caj-new")

    def test_container_apps_job_logs_not_arrived(self):
        cfg = azure("azure_container_app_job", "caj")
        env = "/e"
        ex = [{"name": "caj-new", "properties": {"status": "Succeeded", "startTime": "2026-11-01T08:09:00Z"}}]
        res, _, _ = go(cfg, [cmd(verify.caj_executions_argv(cfg), json.dumps(ex)), cmd(verify.caj_env_argv(cfg), env),
                             cmd(verify.workspace_argv(env), "ws"), cmd(verify.caj_logs_argv("ws", "caj-new"), "[]")])
        self.assertEqual(res["status"], "fail")
        self.assertIn("few minutes", res["reason"])


class OtherKindsTests(unittest.TestCase):
    def test_runtime_path_only_kinds_are_skipped(self):
        cfg = model.default_config("openshift_knative_service", name="t")
        res, _, _ = go(cfg, [])
        self.assertEqual(res["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
