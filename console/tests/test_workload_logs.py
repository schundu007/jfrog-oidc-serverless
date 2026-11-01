"""The workload's own live log around a Prove check, and JFrog's answer as the workload received it.
Recorded answers only; no cloud access."""

import json
import unittest

from console import model
from console.shell import Shell
from console.steps import workload_logs as wl

wl.sleep = lambda s: None

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
GRANTED = {"event": "jfrog_oidc_selftest", "ok": True, "jfrog_status": 200, "provider": "azure-entra",
           "jfrog_user": "azure-entra/aca-runtime", "sub": "5b3d", "message": "JFrog issued a token for azure-entra/aca-runtime via azure-entra, expires in 59 min; read demo-generic/hello.txt: HTTP 200"}
REFUSED = {"event": "jfrog_oidc_selftest", "ok": False, "jfrog_status": 403, "provider": "azure-entra", "sub": "5b3d",
           "message": "JFrog refused the exchange via azure-entra: JFrog OIDC exchange failed: HTTP 403 provider=azure-entra: Forbidden"}


def aca():
    cfg = model.default_config("azure_container_app", name="aca")
    cfg["target"]["azure"].update(subscription="sub", resource_group="rg", name="aca")
    return cfg


def gcp():
    cfg = model.default_config("gcp_run_service", name="svc")
    cfg["target"]["gcp"].update(project="demo", region="us-central1", name="svc")
    return cfg


def aca_lines(*pairs):
    return "\n".join(json.dumps({"TimeStamp": t, "Log": f"F {line}"}) for t, line in pairs) + "\n"


class AcaTests(unittest.TestCase):
    def test_reads_console_log_since_the_check(self):
        out = aca_lines(("2026-11-01T09:00:00.1+00:00", "GET / 404"), ("2026-11-01T09:10:36.0+00:00", "GET / 404"),
                        ("2026-11-01T09:10:36.1+00:00", "Traceback (most recent call last):"),
                        ("2026-11-01T09:10:36.2+00:00", json.dumps(GRANTED)),
                        ("2026-11-01T09:10:36.3+00:00", "GET /healthz/jfrog 200"))
        sh = Shell(mode="replay", fixtures=[{"argv": wl.argv(aca()), "rc": 0, "stdout": out}])
        got = wl.read(aca(), sh, "2026-11-01T09:10:35Z")
        self.assertEqual(got["source"], "Container Apps console log (live)")
        self.assertEqual([r["message"] for r in got["rows"]], [GRANTED["message"], "GET /healthz/jfrog 200"])
        self.assertEqual(got["rows"][0]["level"], "INFO")
        self.assertEqual(got["rows"][0]["at"], "2026-11-01T09:10:36Z")
        self.assertEqual(got["jfrog"], {"status": 200, "answer": GRANTED["message"], "provider": "azure-entra",
                                        "subject": "5b3d", "at": "2026-11-01T09:10:36Z"})

    def test_refusal_is_an_error_row_with_jfrogs_status(self):
        out = aca_lines(("2026-11-01T09:10:41+00:00", json.dumps(REFUSED)), ("2026-11-01T09:10:41+00:00", "GET /healthz/jfrog 503"))
        sh = Shell(mode="replay", fixtures=[{"argv": wl.argv(aca()), "rc": 0, "stdout": out}])
        got = wl.read(aca(), sh, "2026-11-01T09:10:40Z")
        self.assertEqual([r["level"] for r in got["rows"]], ["ERROR", "ERROR"])
        self.assertEqual(got["jfrog"]["status"], 403)

    def test_tokens_never_reach_the_rows(self):
        out = aca_lines(("2026-11-01T09:10:41+00:00", f"GET /healthz/jfrog Authorization: Bearer {JWT}"))
        sh = Shell(mode="replay", fixtures=[{"argv": wl.argv(aca()), "rc": 0, "stdout": out}])
        self.assertNotIn(JWT, json.dumps(wl.read(aca(), sh, "2026-11-01T09:10:40Z")))

    def test_unreadable_log_is_none(self):
        self.assertIsNone(wl.read(aca(), Shell(mode="replay", fixtures=[]), "2026-11-01T09:10:40Z"))


class GcpTests(unittest.TestCase):
    def test_cloud_logging_entries_since_the_check(self):
        entries = [{"timestamp": "2026-11-01T09:10:36.5Z", "severity": "INFO", "jsonPayload": GRANTED},
                   {"timestamp": "2026-11-01T09:10:36.6Z", "severity": "INFO", "textPayload": "GET /healthz/jfrog 200"}]
        argv = wl.argv(gcp(), "2026-11-01T09:10:35Z")
        self.assertIn('resource.labels.service_name="svc"', argv[3])
        self.assertIn('timestamp>="2026-11-01T09:10:35Z"', argv[3])
        sh = Shell(mode="replay", fixtures=[{"argv": argv, "rc": 0, "stdout": json.dumps(entries)}])
        got = wl.read(gcp(), sh, "2026-11-01T09:10:35Z")
        self.assertEqual(got["source"], "Cloud Logging (live)")
        self.assertEqual(len(got["rows"]), 2)
        self.assertEqual(got["jfrog"]["status"], 200)


class AnswerTests(unittest.TestCase):
    def test_answer_from_the_check_when_the_log_has_none(self):
        a = wl.answer_from_check({"status": "fail", "reason": "exchange failed: JFrog OIDC exchange failed: HTTP 403 provider=azure-entra: {\"errors\":[{\"code\":\"FORBIDDEN\",\"message\":\"Forbidden\"}]}"})
        self.assertEqual(a["status"], 403)
        self.assertIn("Forbidden", a["answer"])
        a = wl.answer_from_check({"status": "pass", "jfrog_user": "azure-entra/aca-runtime", "token_lifetime": "60 min"})
        self.assertEqual(a["status"], 200)
        self.assertEqual(a["answer"], "token issued for azure-entra/aca-runtime, expires in 60 min")


if __name__ == "__main__":
    unittest.main()
