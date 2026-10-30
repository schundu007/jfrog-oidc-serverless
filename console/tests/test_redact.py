import json
import os
import unittest
from unittest import mock

from console import redact

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqZmFjQHNpbS91c2Vycy9zdmMifQ.c2lnbmF0dXJlLWJ5dGVz"
REF = "cmVmdGtuOjAxOjE3MzQ1Njc4OTA6YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo"


class RedactTextTests(unittest.TestCase):
    def assertGone(self, secret, text):
        out = redact.text(text)
        self.assertNotIn(secret, out)
        return out

    def test_jwt(self):
        out = self.assertGone(JWT, f"exchanged {JWT} ok")
        self.assertIn("[REDACTED]", out)

    def test_jfrog_reference_token(self):
        self.assertGone(REF, f"token={REF}")

    def test_authorization_header(self):
        out = self.assertGone("s3cr3t-opaque-value", "Authorization: Bearer s3cr3t-opaque-value")
        self.assertIn("Authorization: Bearer [REDACTED]", out)
        self.assertGone("dXNlcjpwYXNz", "authorization=Basic dXNlcjpwYXNz")

    def test_bare_bearer(self):
        self.assertGone("az-at-kv-000017", "sent Bearer az-at-kv-000017 to vault")

    def test_keyvaultref(self):
        out = self.assertGone("https://kv.vault.azure.net/secrets/x", "jfrog-registry-token=keyvaultref:https://kv.vault.azure.net/secrets/x,identityref:/sub/mi")
        self.assertIn("identityref:/sub/mi", out)

    def test_password_flags(self):
        self.assertGone("hunter2", "az acr login --password hunter2")
        self.assertGone("hunter2", "--password=hunter2")
        # --password-stdin takes no value; the next argument is not a secret.
        self.assertIn("--username svc", redact.text("docker login --password-stdin --username svc"))

    def test_basic_auth_url(self):
        self.assertGone("tok3n-value", "https://svc:tok3n-value@acme.jfrog.io/artifactory/api/pypi/simple")

    def test_json_secret_fields(self):
        out = redact.text(json.dumps({"access_token": "opaque-abc-123", "username": "svc"}))
        self.assertNotIn("opaque-abc-123", out)
        self.assertIn('"username": "svc"', out)

    def test_env_token_value(self):
        with mock.patch.dict(os.environ, {"JFROG_ACCESS_TOKEN": "admin-token-XYZ-0001"}):
            self.assertGone("admin-token-XYZ-0001", "leaked admin-token-XYZ-0001 here")

    def test_plain_text_untouched(self):
        s = "rotation_succeeded target=gcp seconds=0.4 version=projects/1/secrets/s/versions/7"
        self.assertEqual(redact.text(s), s)


class RedactQueryAndHeaderTests(unittest.TestCase):
    def test_query_parameters(self):
        for line, secret in (
            ("GET /x?access_token=abcdef123456 HTTP/1.1", "abcdef123456"),
            ("https://orders.azurewebsites.net/api/f?code=FUNCKEYvalue123==&name=a", "FUNCKEYvalue123"),
            ("https://acct.blob.core.windows.net/c/b?sv=2022&sig=SASsig%2Bvalue", "SASsig"),
            ("open http://127.0.0.1:8765/#t=sessiontokenvalue", "sessiontokenvalue"),
        ):
            out = redact.text(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("[REDACTED]", out)
        self.assertIn("name=a", redact.text("https://h/api/f?code=k1234567&name=a"))
        self.assertEqual(redact.text("GET /api/sim/abc?scenario=happy-aca"), "GET /api/sim/abc?scenario=happy-aca")

    def test_google_access_token(self):
        out = redact.text("token ya29.a0AfB_byC-abcdefghijklmnop1234 expires")
        self.assertEqual(out, "token [REDACTED] expires")

    def test_secret_headers(self):
        for line in ("x-functions-key: FUNCKEY123456", "X-Console-Token: sesstoken1234", "x-identity-header=idh123456"):
            out = redact.text(line)
            self.assertIn("[REDACTED]", out, line)
            self.assertNotRegex(out, r"FUNCKEY123456|sesstoken1234|idh123456")


class RedactStructureTests(unittest.TestCase):
    def test_nested(self):
        obj = {"a": [JWT, {"b": f"Bearer {JWT}"}], "n": 3, "ok": True}
        out = redact.obj(obj)
        self.assertNotIn(JWT, json.dumps(out))
        self.assertEqual(out["n"], 3)
        self.assertIs(out["ok"], True)

    def test_secret_keys_masked(self):
        out = redact.obj({"access_token": "x", "IDENTITY_HEADER": "h", "subject_token": "y", "username": "u"})
        self.assertEqual(out["access_token"], "[REDACTED]")
        self.assertEqual(out["IDENTITY_HEADER"], "[REDACTED]")
        self.assertEqual(out["subject_token"], "[REDACTED]")
        self.assertEqual(out["username"], "u")

    def test_argv(self):
        argv = ["az", "containerapp", "registry", "set", "--password", "p@ss", "--username", "svc"]
        self.assertEqual(redact.argv(argv)[5], "[REDACTED]")
        self.assertEqual(redact.argv(["x", "--password=p@ss"])[1], "--password=[REDACTED]")
        self.assertEqual(redact.argv(["docker", "login", "--password-stdin", "h"])[3], "h")


class RunLogPathTests(unittest.TestCase):
    """M4 log paths: run events (SSE), runs/<id>/events.jsonl and the run.json snapshot."""

    def test_run_events_file_and_snapshot(self):
        import tempfile
        from pathlib import Path

        from console.runner import Run
        with tempfile.TemporaryDirectory() as tmp:
            run = Run({"name": "t", "target": {"kind": "gcp_run_service"}}, ["plan"], Path(tmp))
            run.log(f"$ terraform apply  Authorization: Bearer {JWT}")
            run.emit("evidence", detail=f"exchanged {JWT}", access_token=REF)
            run.set_step("plan", status="failed", error=f"terraform failed: token={REF}",
                         evidence={"note": f"subject {JWT}", "function_key": "FUNCKEY123456"})
            on_disk = (run.dir / "events.jsonl").read_text()
            for blob in (json.dumps(run.events), on_disk, json.dumps(run.snapshot())):
                self.assertNotIn(JWT, blob)
                self.assertNotIn(REF, blob)
                self.assertNotIn("FUNCKEY123456", blob)
            self.assertIn("[REDACTED]", on_disk)


if __name__ == "__main__":
    unittest.main()
