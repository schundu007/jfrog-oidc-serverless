"""Emulated AWS: the container credentials endpoint and STS GetWebIdentityToken.

Paths (under the /aws prefix):
    GET  /creds/<workload>   container credentials (ECS task role, App Runner instance role);
                             needs the workload's Authorization value, like
                             AWS_CONTAINER_CREDENTIALS_FULL_URI + AWS_CONTAINER_AUTHORIZATION_TOKEN
    POST /sts/               the STS Query API: Action=GetWebIdentityToken, SigV4-signed

STS here checks what the real one does that matters to the client: the SigV4
signature (recomputed with the workload's secret, via the client's own sigv4
module), the session token, the form fields, the regional-only rule (region in
the credential scope), and outbound federation being enabled for the account.
It answers in STS's XML. The JWT is HS256 (the JFrog emulator trusts the key);
iss is the account's issuer https://<id>.tokens.sts.global.api.aws and sub the
role ARN, as in the AWS docs' example.
"""

from __future__ import annotations

import calendar
import secrets
import time
import urllib.parse

from . import jwt
from .server import Request

NS = "https://sts.amazonaws.com/doc/2011-06-15/"
ACCOUNT = "123456789012"
ISSUER_ID = "a1d2b0fd-1177-4468-9351-2feeeeeee723"


def _xml_err(status: int, code: str, message: str):
    body = (f'<ErrorResponse xmlns="{NS}"><Error><Type>Sender</Type><Code>{code}</Code>'
            f"<Message>{message}</Message></Error><RequestId>sim</RequestId></ErrorResponse>")
    return status, body, {"Content-Type": "text/xml"}


class AwsEmulator:
    def __init__(self, clock, key: bytes, region: str = "us-east-1", account: str = ACCOUNT):
        self.clock = clock
        self.key = key
        self.region = region
        self.account = account
        self.issuer = f"https://{ISSUER_ID}.tokens.sts.global.api.aws"
        self.federation_enabled = True
        self.workloads: dict = {}   # workload -> {role, auth, down}
        self._keys: dict = {}       # access key id -> {secret, session, workload}
        self.host = ""              # set by the engine once the server is up (SigV4 signs the Host)
        self.issued = 0

    # -- configuration -------------------------------------------------------

    def add_workload(self, workload: str, role_name: str) -> dict:
        """A workload whose role is <role_name>. Returns its static credentials (Lambda's env vars)."""
        self.workloads[workload] = {"role": f"arn:aws:iam::{self.account}:role/{role_name}",
                                    "auth": secrets.token_hex(12), "down": False}
        return self._mint_keys(workload)

    def recreate_role(self, workload: str) -> None:
        """Delete and recreate the role with the same name: same ARN, new role ID (sub is the ARN)."""
        self.workloads[workload]["role_id"] = "AROA" + secrets.token_hex(8).upper()

    def _mint_keys(self, workload: str) -> dict:
        akid = "ASIA" + secrets.token_hex(8).upper()
        self._keys[akid] = {"secret": secrets.token_urlsafe(30), "session": secrets.token_urlsafe(24),
                            "workload": workload}
        return {"AccessKeyId": akid, "SecretAccessKey": self._keys[akid]["secret"],
                "Token": self._keys[akid]["session"]}

    # -- HTTP ----------------------------------------------------------------

    def handle(self, req: Request):
        p = req.path
        if p.startswith("/creds/") and req.method == "GET":
            return self._creds(req, p[len("/creds/"):].strip("/"))
        if p in ("/sts", "/sts/") and req.method == "POST":
            return self._sts(req)
        return 404, {"message": f"not found: {p}"}, {}

    def _creds(self, req: Request, workload: str):
        w = self.workloads.get(workload)
        if w is None:
            return 404, {"message": f"no credentials endpoint for {workload}"}, {}
        if req.headers.get("authorization") != w["auth"]:
            return 401, {"message": "Authorization token missing or invalid"}, {}
        if w["down"]:
            return 503, {"message": "credentials endpoint unavailable"}, {}
        creds = self._mint_keys(workload)
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.now() + 6 * 3600))
        return 200, {**creds, "RoleArn": w["role"], "Expiration": exp}, {}

    def _sts(self, req: Request):
        from jfrog_oidc import sigv4  # the client's own signer: the check is only as good as the vectors it passed

        auth = req.headers.get("authorization", "")
        try:
            cred = auth.split("Credential=", 1)[1].split(",", 1)[0]
            akid, _date, region, service, _term = cred.split("/")
            signed = auth.split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")
            now = calendar.timegm(time.strptime(req.headers["x-amz-date"], "%Y%m%dT%H%M%SZ"))
        except (IndexError, KeyError, ValueError):
            return _xml_err(403, "IncompleteSignature", "Authorization header requires Credential, SignedHeaders and X-Amz-Date")
        key = self._keys.get(akid)
        if key is None:
            return _xml_err(403, "InvalidClientTokenId", "The security token included in the request is invalid.")
        extra = {k: v for k, v in req.headers.items() if k in signed and k not in ("host", "x-amz-date", "x-amz-security-token")}
        want = sigv4.sign("POST", f"http://{req.headers.get('host', self.host)}/aws{req.path}", extra, req.body,
                          access_key=akid, secret_key=key["secret"], region=region, service=service,
                          session_token=req.headers.get("x-amz-security-token"), now=now)
        if service != "sts" or not secrets.compare_digest(auth, want["Authorization"]):
            return _xml_err(403, "SignatureDoesNotMatch", "The request signature we calculated does not match the signature you provided.")
        if req.headers.get("x-amz-security-token") != key["session"]:
            return _xml_err(403, "InvalidClientTokenId", "The security token included in the request is invalid.")
        form = dict(urllib.parse.parse_qsl(req.body.decode()))
        if form.get("Action") != "GetWebIdentityToken" or form.get("Version") != "2011-06-15":
            return _xml_err(400, "InvalidAction", f"Could not find operation {form.get('Action')} for version {form.get('Version')}")
        if region != self.region:
            return _xml_err(403, "SignatureDoesNotMatch", f"Credential should be scoped to a valid region, not '{region}'.")
        if not self.federation_enabled:
            return _xml_err(403, "OutboundWebIdentityFederationDisabledException",
                            f"Outbound identity federation is disabled for account {self.account}")
        auds = [v for k, v in sorted(form.items()) if k.startswith("Audience.member.")]
        if not 1 <= len(auds) <= 10:
            return _xml_err(400, "ValidationError", "Audience must contain between 1 and 10 entries")
        if form.get("SigningAlgorithm") not in ("RS256", "ES384"):
            return _xml_err(400, "ValidationError", "SigningAlgorithm must be RS256 or ES384")
        duration = int(form.get("DurationSeconds") or 300)
        if not 60 <= duration <= 3600:
            return _xml_err(400, "ValidationError", "DurationSeconds must be between 60 and 3600")
        now_v = int(self.clock.now())
        claims = {"iss": self.issuer, "sub": self.workloads[key["workload"]]["role"],
                  "aud": auds[0] if len(auds) == 1 else auds, "iat": now_v, "nbf": now_v, "exp": now_v + duration,
                  "jti": secrets.token_hex(8),
                  "https://sts.amazonaws.com/": {"aws_account": self.account, "source_region": self.region}}
        self.issued += 1
        token = jwt.encode(claims, self.key)
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_v + duration))
        body = (f'<GetWebIdentityTokenResponse xmlns="{NS}"><GetWebIdentityTokenResult>'
                f"<WebIdentityToken>{token}</WebIdentityToken><Expiration>{exp}</Expiration>"
                "</GetWebIdentityTokenResult><ResponseMetadata><RequestId>sim</RequestId></ResponseMetadata>"
                "</GetWebIdentityTokenResponse>")
        return 200, body, {"Content-Type": "text/xml"}
