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

    def test_selftest_without_path_skips_read(self):
        result = probe.selftest(provider=self.gcp_provider())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["read"])


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
