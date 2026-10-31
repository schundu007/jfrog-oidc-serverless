"""No stored JFrog token: OperatorToken mints a short-lived one by OIDC exchange (gcloud ID token
for the operator service account → /access/api/v1/oidc/token), caches it in memory only, and
never puts it in argv or a log line."""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from kcp_console.jfrog import Jfrog, OperatorToken, TokenError, operator_identity
from kcp_console.shell import HttpResult, Result

ENV = {"JFROG_OIDC_PROVIDER": "gcp-cloudrun", "JFROG_OIDC_AUDIENCE": "jfrog-gcp",
       "JFROG_OIDC_SERVICE_ACCOUNT": "jfrog-console-operator@p.iam.gserviceaccount.com"}
STATE_DIR = tempfile.mkdtemp()
STATE = pathlib.Path(STATE_DIR, "terraform.tfstate")  # gcp-foundation's state, as far as the console reads it
STATE.write_text(json.dumps({"outputs": {"console_operator_env": {"value": ENV}}}))
ID_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
ACCESS = "eyJ2ZXIiOiIyIn0.eyJzdWIiOiJqZmFjIn0.YWNjZXNz"


class FakeShell:
    def __init__(self, id_rc=0, status=200):
        self.id_rc, self.status = id_rc, status
        self.calls, self.http_calls = [], []

    def run(self, argv, timeout=60.0, sensitive=False, extra_env=None, stream=False):
        self.calls.append((list(argv), sensitive))
        return Result(list(argv), self.id_rc, ID_TOKEN + "\n" if self.id_rc == 0 else "", "denied", 0.0)

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        self.http_calls.append((method, url, dict(headers or {}), body, sensitive))
        if url.endswith("/access/api/v1/oidc/token"):
            ok = self.status == 200
            return HttpResult(method, url, self.status, json.dumps({"access_token": ACCESS, "expires_in": 900}) if ok else "no")
        return HttpResult(method, url, 200, "[]")


class OperatorTokenTests(unittest.TestCase):
    def test_mints_by_exchange_and_sends_it_as_a_header(self):
        sh = FakeShell()
        jf = Jfrog(sh, "x.jfrog.io", tokens=OperatorToken(state_file=STATE))
        jf.providers()
        argv, sensitive = sh.calls[0]
        self.assertEqual(argv[:3], ["gcloud", "auth", "print-identity-token"])
        self.assertIn("--impersonate-service-account=" + ENV["JFROG_OIDC_SERVICE_ACCOUNT"], argv)
        self.assertIn("--audiences=jfrog-gcp", argv)
        self.assertTrue(sensitive)
        ex = sh.http_calls[0]
        self.assertEqual(ex[1], "https://x.jfrog.io/access/api/v1/oidc/token")
        self.assertTrue(ex[4])
        body = json.loads(ex[3])
        self.assertEqual((body["provider_name"], body["subject_token"]), ("gcp-cloudrun", ID_TOKEN))
        self.assertEqual(sh.http_calls[1][2]["Authorization"], f"Bearer {ACCESS}")
        for argv, _ in sh.calls:  # the token never rides in argv
            self.assertNotIn(ACCESS, " ".join(argv))

    def test_cached_until_shortly_before_expiry(self):
        now = [1000.0]
        tokens = OperatorToken(clock=lambda: now[0], state_file=STATE)
        sh = FakeShell()
        tokens.get(sh, "x.jfrog.io")
        tokens.get(sh, "x.jfrog.io")
        self.assertEqual(len(sh.calls), 1)
        now[0] += 900 - 60
        tokens.get(sh, "x.jfrog.io")
        self.assertEqual(len(sh.calls), 2)

    def test_identity_follows_the_state_file(self):
        import shutil, tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        state = pathlib.Path(d, "terraform.tfstate")
        tokens = OperatorToken(state_file=state)
        self.assertFalse(tokens.configured)   # gcp-foundation not deployed yet
        shutil.copy(STATE, state)
        self.assertTrue(tokens.configured)    # deployed while the console runs
        tokens.get(FakeShell(), "x.jfrog.io")
        state.write_text('{"outputs": {}}')   # destroyed: no stale identity or cached token
        with self.assertRaises(TokenError):
            tokens.get(FakeShell(), "x.jfrog.io")

    def test_not_configured_names_the_settings(self):
        with self.assertRaises(TokenError) as cm:
            OperatorToken(state_file=pathlib.Path(STATE_DIR, "missing")).get(FakeShell(), "x.jfrog.io")
        self.assertIn("gcp-foundation", str(cm.exception))

    def test_the_identity_is_the_gcp_foundation_output(self):
        self.assertEqual(operator_identity(STATE), ENV)
        self.assertEqual(operator_identity(pathlib.Path(STATE_DIR, "missing"))["JFROG_OIDC_PROVIDER"], "")

    def test_a_failed_exchange_says_so_and_the_call_goes_out_anonymous(self):
        sh = FakeShell(status=401)
        jf = Jfrog(sh, "x.jfrog.io", tokens=OperatorToken(state_file=STATE))
        self.assertIn("HTTP 401", jf.token_error())
        jf.providers()
        self.assertNotIn("Authorization", sh.http_calls[-1][2])

    def test_gcloud_refusal_names_the_role(self):
        jf = Jfrog(FakeShell(id_rc=1), "x.jfrog.io", tokens=OperatorToken(state_file=STATE))
        self.assertIn("serviceAccountTokenCreator", jf.token_error())

    def test_the_stored_token_variable_is_ignored(self):
        sh = FakeShell()
        with mock.patch.dict(os.environ, {"JFROG_ACCESS_TOKEN": "stored-admin-token"}):
            jf = Jfrog(sh, "x.jfrog.io", tokens=OperatorToken(state_file=pathlib.Path(STATE_DIR, "missing")))
            jf.providers()
        self.assertNotIn("Authorization", sh.http_calls[-1][2])


if __name__ == "__main__":
    unittest.main()
