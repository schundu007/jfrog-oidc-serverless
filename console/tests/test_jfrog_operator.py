"""console/jfrog_operator.py: the operator's short-lived JFrog token, per cloud, never stored or logged."""

import json
import tempfile
import unittest
from pathlib import Path

from console import jfrog_operator
from console.shell import HttpResult, Result, Shell

ENTRA = "eyJhbGciOiJSUzI1NiJ9.eyJvaWQiOiJ4In0.c2ln"   # never logged or returned
JF = "jfrog-access-token-value"


def state(tmp, env):
    p = Path(tmp) / "terraform.tfstate"
    p.write_text(json.dumps({"outputs": {"console_operator_env": {"value": env}}}))
    return p


class FakeSh(Shell):
    def __init__(self, az_ok=True):
        super().__init__(mode="replay", log=self.lines_append)
        self.lines, self.cmds, self.posts, self.az_ok = [], [], [], az_ok

    def lines_append(self, line):
        self.lines.append(line)

    def _replay_cmd(self, argv):
        self.cmds.append(list(argv))
        return Result(argv, 0 if self.az_ok else 1, ENTRA + "\n" if self.az_ok else "", "" if self.az_ok else "AADSTS50105", 0.0)

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        self.posts.append((url, json.loads(body) if body else None))
        return HttpResult(method, url, 200, json.dumps({"access_token": JF, "expires_in": 900}))


AZ_ENV = {"JFROG_OIDC_PROVIDER": "console-operator-entra", "JFROG_OIDC_AUDIENCE": "api://jfrog-console-contoso",
          "JFROG_OIDC_SOURCE": "azure"}


class AzureOperatorTests(unittest.TestCase):
    def test_azure_operator_exchanges_its_entra_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            sh = FakeSh()
            op = jfrog_operator.Operator(sh, "acme.jfrog.io", cloud="azure", state_file=state(tmp, AZ_ENV))
            self.assertTrue(op.configured)
            self.assertEqual(op._token(), JF)
            self.assertEqual(sh.cmds[0][:3], ["az", "account", "get-access-token"])
            self.assertIn("api://jfrog-console-contoso/.default", sh.cmds[0])
            url, body = sh.posts[0]
            self.assertEqual(url, "https://acme.jfrog.io/access/api/v1/oidc/token")
            self.assertEqual((body["provider_name"], body["subject_token"]), ("console-operator-entra", ENTRA))
            self.assertEqual(op._token(), JF)          # cached
            self.assertEqual(len(sh.cmds), 1)
            self.assertFalse(any(ENTRA in l or JF in l for l in sh.lines))

    def test_tokens_pick_the_clouds_operator_and_cache_per_cloud_and_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            sh = FakeSh()
            files = {"azure": state(tmp, AZ_ENV), "gcp": Path(tmp) / "none"}
            toks = jfrog_operator.Tokens(state_file_for=files.get)
            self.assertEqual(toks.get(sh, "acme.jfrog.io", "azure"), JF)
            self.assertEqual(toks.get(FakeSh(), "acme.jfrog.io", "azure"), JF)
            self.assertEqual(len(sh.cmds), 1)                      # cached across shells
            with self.assertRaisesRegex(jfrog_operator.OperatorError, "gcp-jfrog-trust"):
                toks.get(sh, "acme.jfrog.io", "gcp")

    def test_azure_reads_its_own_trust_stack_never_gcps(self):
        self.assertIn("oidc/azure/azure-jfrog-trust", str(jfrog_operator.state_file("azure")))
        self.assertIn("oidc/gcp/gcp-jfrog-trust", str(jfrog_operator.state_file("gcp")))

    def test_missing_identity_names_the_clouds_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            op = jfrog_operator.Operator(FakeSh(), "h", cloud="azure", state_file=Path(tmp) / "none")
            self.assertFalse(op.configured)
            with self.assertRaisesRegex(jfrog_operator.OperatorError, "azure-jfrog-trust"):
                op._token()

    def test_az_refusal_is_an_error_without_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            op = jfrog_operator.Operator(FakeSh(az_ok=False), "h", cloud="azure", state_file=state(tmp, AZ_ENV))
            with self.assertRaisesRegex(jfrog_operator.OperatorError, "az could not get a token"):
                op._token()


if __name__ == "__main__":
    unittest.main()
