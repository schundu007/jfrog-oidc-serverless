"""Tests for jfrog_oidc.probe: the self-test used by /healthz/jfrog and the console probes.

Run: PYTHONPATH=src python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from jfrog_oidc import probe

from test_jfrog_oidc import BaseCase, FakeCloud


class SelfTestTests(BaseCase):
    def test_selftest_reports_claims_check_and_read(self):
        p = self.gcp_provider()
        result = probe.selftest("generic-local/healthcheck/ping.txt", provider=p)
        self.assertTrue(result["ok"])
        self.assertEqual(result["claims"]["sub"], "1234567890")
        self.assertEqual(result["claims"]["aud"], "jfrog-gcp")
        self.assertEqual(result["check"]["username"], "cr-orders")
        self.assertEqual(result["check"]["expires_in"], 3600)
        self.assertEqual(result["check"]["provider"], "gcp-cloudrun")
        self.assertEqual(result["read"], {"path": "generic-local/healthcheck/ping.txt", "status": 200})

    def test_selftest_never_returns_a_token(self):
        p = self.gcp_provider()
        result = probe.selftest("generic-local/healthcheck/ping.txt", provider=p)
        blob = json.dumps(result)
        self.assertNotIn(FakeCloud.state["current_access"], blob)
        self.assertNotIn("eyJ", blob)  # no JWT of any kind, identity or access

    def test_selftest_read_failure_is_not_ok(self):
        result = probe.selftest("other-repo/secret.txt", provider=self.gcp_provider())
        self.assertFalse(result["ok"])
        self.assertEqual(result["read"]["status"], 404)

    def test_selftest_exchange_failure_is_reported(self):
        FakeCloud.state["provider"] = "something-else"  # JFrog rejects provider_name
        result = probe.selftest("generic-local/healthcheck/ping.txt", provider=self.gcp_provider())
        self.assertFalse(result["ok"])
        self.assertIn("401", result["check"]["error"])
        self.assertIsNone(result["read"])
        self.assertEqual(result["claims"]["sub"], "1234567890")  # claims still shown for debugging

    def test_fresh_selftest_exchanges_again_and_sees_a_removed_trust(self):
        p = self.gcp_provider()
        self.assertTrue(probe.selftest(provider=p)["ok"])          # token now cached
        FakeCloud.state["provider"] = "something-else"             # JFrog stops trusting this identity
        self.assertTrue(probe.selftest(provider=p)["ok"])          # the cached token still answers
        fresh = probe.selftest(provider=p, fresh=True)
        self.assertFalse(fresh["ok"])
        self.assertIn("401", fresh["check"]["error"])

    def test_selftest_without_path_skips_read(self):
        result = probe.selftest(provider=self.gcp_provider())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["read"])


class LogResultTests(BaseCase):
    def _line(self, result):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            probe.log_result(result)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def test_granted_line_names_user_lifetime_and_read(self):
        line = self._line(probe.selftest("generic-local/healthcheck/ping.txt", provider=self.gcp_provider(), fresh=True))
        self.assertEqual(line["event"], "jfrog_oidc_selftest")
        self.assertTrue(line["ok"])
        self.assertEqual(line["jfrog_status"], 200)
        self.assertIn("JFrog issued a token for cr-orders", line["message"])
        self.assertIn("read generic-local/healthcheck/ping.txt: HTTP 200", line["message"])
        self.assertEqual(line["sub"], "1234567890")

    def test_refused_line_carries_jfrogs_answer(self):
        FakeCloud.state["provider"] = "something-else"
        line = self._line(probe.selftest(provider=self.gcp_provider(), fresh=True))
        self.assertFalse(line["ok"])
        self.assertEqual(line["jfrog_status"], 401)
        self.assertIn("JFrog refused the exchange", line["message"])

    def test_line_never_holds_a_token(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            probe.log_result(probe.selftest("generic-local/healthcheck/ping.txt", provider=self.gcp_provider()))
        self.assertNotIn(FakeCloud.state["current_access"], out.getvalue())
        self.assertNotIn("eyJ", out.getvalue())


class ProbeCliTests(BaseCase):
    def run_cli(self, *argv):
        env = {
            "JFROG_URL": self.base,
            "JFROG_OIDC_PROVIDER": "gcp-cloudrun",
            "JFROG_OIDC_AUDIENCE": "jfrog-gcp",
            "JFROG_OIDC_SOURCE": "gcp",
            "GCE_METADATA_HOST": self.base,
        }
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            rc = probe.main(list(argv))
        return rc, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def test_read_ok(self):
        rc, lines = self.run_cli("read", "generic-local/healthcheck/ping.txt")
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1], {"event": "probe_read", "path": "generic-local/healthcheck/ping.txt", "status": 200})

    def test_read_forbidden_exits_nonzero(self):
        rc, lines = self.run_cli("read", "other-repo/x")
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["status"], 404)

    def test_selftest_cli_prints_json_without_token(self):
        rc, lines = self.run_cli("selftest", "generic-local/healthcheck/ping.txt")
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1]["event"], "probe_selftest")
        self.assertTrue(lines[-1]["ok"])
        self.assertNotIn(FakeCloud.state["current_access"], json.dumps(lines))


if __name__ == "__main__":
    unittest.main()
