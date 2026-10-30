"""AwsStsSource against a local fake of STS and the container credentials endpoint.

The fake checks the SigV4 signature the way STS does (recomputes it with the
known secret), the Query API form fields, and answers in STS's XML.
"""

import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from jfrog_oidc import AwsStsSource, IdentityError, JFrogTokenProvider, sigv4
from jfrog_oidc.sources import detect_source

NOW = 1_767_225_600.0     # 2026-01-01T00:00:00Z
ROLE = "arn:aws:iam::123456789012:role/orders-api"
ISSUER = "https://a1d2b0fd-1177-4468-9351-2feeeeeee723.tokens.sts.global.api.aws"
KEYS = {"AKIALAMBDA": "lambda-secret-key/EXAMPLE", "ASIACONTAINER": "container-secret-key/EXAMPLE"}
NS = "https://sts.amazonaws.com/doc/2011-06-15/"


def _jwt(claims):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256', 'kid': 'k1'})}.{enc(claims)}.sig"


class FakeAws(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/xml"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        st = self.state
        if self.path.startswith("/creds"):
            if st.get("creds_auth") and self.headers.get("Authorization") != st["creds_auth"]:
                return self._send(401, {"message": "bad token"}, "application/json")
            return self._send(200, {"AccessKeyId": "ASIACONTAINER", "SecretAccessKey": KEYS["ASIACONTAINER"],
                                    "Token": "container-session", "Expiration": "2026-01-01T06:00:00Z"},
                              "application/json")
        self._send(404, "nope")

    def do_POST(self):
        st = self.state
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        form = dict(urllib.parse.parse_qsl(body.decode()))
        st["calls"].append(form)
        auth = self.headers.get("Authorization", "")
        akid = auth.split("Credential=", 1)[1].split("/", 1)[0] if "Credential=" in auth else ""
        signed = [h for h in auth.split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")] if auth else []
        want = sigv4.sign("POST", f"http://{self.headers['Host']}/",
                          {k: self.headers[k] for k in self.headers if k.lower() in signed
                           and k.lower() not in ("host", "x-amz-date", "x-amz-security-token")},
                          body, access_key=akid, secret_key=KEYS.get(akid, "?"), region="eu-west-1", service="sts",
                          session_token=self.headers.get("X-Amz-Security-Token"), now=NOW)
        if auth != want["Authorization"]:
            return self._send(403, f'<ErrorResponse xmlns="{NS}"><Error><Code>SignatureDoesNotMatch</Code>'
                                   '<Message>bad signature</Message></Error></ErrorResponse>')
        if st.get("disabled"):
            return self._send(403, f'<ErrorResponse xmlns="{NS}"><Error><Type>Sender</Type>'
                                   '<Code>OutboundWebIdentityFederationDisabledException</Code>'
                                   '<Message>Outbound web identity federation is disabled</Message></Error></ErrorResponse>')
        assert form["Action"] == "GetWebIdentityToken" and form["Version"] == "2011-06-15"
        tok = _jwt({"iss": ISSUER, "sub": ROLE, "aud": form["Audience.member.1"], "exp": int(NOW) + 900})
        return self._send(200, f'<GetWebIdentityTokenResponse xmlns="{NS}"><GetWebIdentityTokenResult>'
                               f'<WebIdentityToken>{tok}</WebIdentityToken><Expiration>2026-01-01T00:15:00Z</Expiration>'
                               '</GetWebIdentityTokenResult></GetWebIdentityTokenResponse>')


class AwsSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAws)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeAws.state = {"calls": []}
        self.env = mock.patch.dict(os.environ, {"JFROG_OIDC_ALLOW_HTTP": "1", "AWS_REGION": "eu-west-1",
                                                "AWS_ENDPOINT_URL_STS": self.base}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def claims(self, token):
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))

    def test_lambda_env_credentials(self):
        os.environ.update(AWS_ACCESS_KEY_ID="AKIALAMBDA", AWS_SECRET_ACCESS_KEY=KEYS["AKIALAMBDA"],
                          AWS_SESSION_TOKEN="lambda-session")
        tok = AwsStsSource(clock=lambda: NOW).get_token("jfrog-aws")
        c = self.claims(tok)
        self.assertEqual((c["iss"], c["sub"], c["aud"]), (ISSUER, ROLE, "jfrog-aws"))
        call = FakeAws.state["calls"][0]
        self.assertEqual(call["SigningAlgorithm"], "RS256")
        self.assertEqual(call["DurationSeconds"], "900")

    def test_container_credentials_full_uri_with_token_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False) as fh:
            fh.write("container-auth\n")
        self.addCleanup(os.unlink, fh.name)
        FakeAws.state["creds_auth"] = "container-auth"
        os.environ.update(AWS_CONTAINER_CREDENTIALS_FULL_URI=f"{self.base}/creds",
                          AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE=fh.name)
        self.assertEqual(self.claims(AwsStsSource(clock=lambda: NOW).get_token("jfrog-aws"))["sub"], ROLE)

    def test_container_credentials_relative_uri(self):
        os.environ["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] = "/creds/v2/abc"
        with mock.patch.object(AwsStsSource, "ECS_HOST", self.base):
            self.assertEqual(self.claims(AwsStsSource(clock=lambda: NOW).get_token("jfrog-aws"))["aud"], "jfrog-aws")

    def test_full_uri_must_be_local(self):
        os.environ["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = "http://evil.example/creds"
        with self.assertRaisesRegex(IdentityError, "local credentials endpoint"):
            AwsStsSource(clock=lambda: NOW).get_token("jfrog-aws")

    def test_federation_disabled_names_the_fix(self):
        FakeAws.state["disabled"] = True
        os.environ.update(AWS_ACCESS_KEY_ID="AKIALAMBDA", AWS_SECRET_ACCESS_KEY=KEYS["AKIALAMBDA"])
        with self.assertRaises(IdentityError) as ctx:
            AwsStsSource(clock=lambda: NOW).get_token("jfrog-aws")
        msg = str(ctx.exception)
        self.assertIn("OutboundWebIdentityFederationDisabledException", msg)
        self.assertIn("aws iam enable-outbound-web-identity-federation", msg)
        self.assertNotIn(KEYS["AKIALAMBDA"], msg)

    def test_bad_signature_is_reported_without_secrets(self):
        os.environ.update(AWS_ACCESS_KEY_ID="AKIALAMBDA", AWS_SECRET_ACCESS_KEY="wrong-secret")
        with self.assertRaisesRegex(IdentityError, "SignatureDoesNotMatch") as ctx:
            AwsStsSource(clock=lambda: NOW).get_token("jfrog-aws")
        self.assertNotIn("wrong-secret", str(ctx.exception))

    def test_needs_region_and_credentials(self):
        del os.environ["AWS_REGION"]
        with self.assertRaisesRegex(IdentityError, "region"):
            AwsStsSource()
        os.environ["AWS_REGION"] = "eu-west-1"
        with self.assertRaisesRegex(IdentityError, "no AWS credentials"):
            AwsStsSource().get_token("jfrog-aws")

    def test_default_endpoint_is_regional_https(self):
        del os.environ["AWS_ENDPOINT_URL_STS"]
        self.assertEqual(AwsStsSource()._endpoint, "https://sts.eu-west-1.amazonaws.com")

    def test_plain_http_needs_opt_in(self):
        del os.environ["JFROG_OIDC_ALLOW_HTTP"]
        with self.assertRaisesRegex(IdentityError, "https"):
            AwsStsSource()

    def test_detect_lambda_and_ecs(self):
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "orders"
        self.assertIsInstance(detect_source(), AwsStsSource)
        del os.environ["AWS_LAMBDA_FUNCTION_NAME"]
        os.environ["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] = "/v2/creds"
        self.assertIsInstance(detect_source(), AwsStsSource)
        os.environ["JFROG_OIDC_SOURCE"] = "aws"
        self.assertIsInstance(detect_source(), AwsStsSource)

    def test_provider_end_to_end_source(self):
        os.environ.update(AWS_ACCESS_KEY_ID="AKIALAMBDA", AWS_SECRET_ACCESS_KEY=KEYS["AKIALAMBDA"],
                          JFROG_URL="https://acme.jfrog.io", JFROG_OIDC_PROVIDER="aws-123456789012",
                          JFROG_OIDC_AUDIENCE="jfrog-aws", JFROG_OIDC_SOURCE="aws")
        p = JFrogTokenProvider.from_env()
        self.assertIsInstance(p.source, AwsStsSource)


if __name__ == "__main__":
    unittest.main()
