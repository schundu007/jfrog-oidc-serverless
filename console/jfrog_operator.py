"""The console operator's own short-lived JFrog token, by OIDC exchange (no stored token).

Each cloud has its own operator, made by its own JFrog trust stack (clouds share nothing):
- GCP: gcp-jfrog-trust's `console_operator_env` names a Google service account, the audience and the
  JFrog provider; the operator's gcloud login impersonates that account for an ID token.
- Azure: azure-jfrog-trust's `console_operator_env` names an Entra application (audience) and the JFrog
  provider; the operator's az login gets an access token for it (only assigned operators can).
The cloud token is sensitive (never logged or recorded) and is exchanged at JFrog. The JFrog token lives in memory only, cached per host until
two minutes before it expires. Used only by Prove, whose JFrog writes sit behind its approval.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

EXCHANGE_PATH = "/access/api/v1/oidc/token"
REFRESH_BEFORE = 120
IDENTITY_KEYS = ("JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SERVICE_ACCOUNT")
AZURE_KEYS = ("JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE")
ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "terraform/stacks/oidc/gcp/gcp-jfrog-trust/terraform.tfstate"
STACKS = {"gcp": "gcp-jfrog-trust", "azure": "azure-jfrog-trust"}


def state_file(cloud: str) -> pathlib.Path:
    return ROOT / f"terraform/stacks/oidc/{cloud}/{STACKS.get(cloud, STACKS['gcp'])}/terraform.tfstate"


class OperatorError(RuntimeError):
    pass


def identity(state_file=STATE, keys=IDENTITY_KEYS) -> dict:
    try:
        out = json.loads(pathlib.Path(state_file).read_text())["outputs"]["console_operator_env"]["value"] or {}
    except (OSError, ValueError, KeyError, TypeError):
        out = {}
    return {k: str(out.get(k) or "") for k in keys}


class Operator:
    """JFrog REST calls as the console operator: call(method, path, body) -> (status, json or None)."""

    def __init__(self, shell, host: str, state_file=None, clock=time.time, cloud: str = "gcp"):
        self.sh, self.host, self.clock = shell, host, clock
        self.cloud = "azure" if cloud == "azure" else "gcp"
        self.ident = identity(state_file or globals()["state_file"](self.cloud),
                              AZURE_KEYS if self.cloud == "azure" else IDENTITY_KEYS)
        self._tok, self._exp = "", 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return all(self.ident.values())

    def _token(self) -> str:
        if not self.configured:
            raise OperatorError(f"no console operator identity: deploy {STACKS[self.cloud]} with console_operators")
        with self._lock:
            if self._tok and self._exp - REFRESH_BEFORE > self.clock():
                return self._tok
            aud, prov = self.ident["JFROG_OIDC_AUDIENCE"], self.ident["JFROG_OIDC_PROVIDER"]
            if self.cloud == "azure":
                r = self.sh.run(["az", "account", "get-access-token", "--scope", f"{aud}/.default",
                                 "--query", "accessToken", "-o", "tsv"], timeout=60, sensitive=True)
                subject = r.stdout.strip() if r.ok else ""
                if not subject:
                    raise OperatorError(f"az could not get a token for {aud} (is your login one of console_operators?)")
                kind = "urn:ietf:params:oauth:token-type:id_token"   # what JFrog takes for Entra tokens, as client/ does
            else:
                sa = self.ident["JFROG_OIDC_SERVICE_ACCOUNT"]
                r = self.sh.run(["gcloud", "auth", "print-identity-token", f"--impersonate-service-account={sa}",
                                 f"--audiences={aud}", "--include-email"], timeout=60, sensitive=True)
                subject = r.stdout.strip() if r.ok else ""
                if not subject:
                    raise OperatorError(f"gcloud could not get an ID token as {sa}")
                kind = "urn:ietf:params:oauth:token-type:id_token"
            body = json.dumps({"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                               "subject_token_type": kind,
                               "subject_token": subject, "provider_name": prov}).encode()
            res = self.sh.http("POST", f"https://{self.host}{EXCHANGE_PATH}", body=body, sensitive=True,
                               headers={"Content-Type": "application/json"})
            data = res.json() if res.ok else None
            tok = data.get("access_token") if isinstance(data, dict) else None
            if not tok:
                raise OperatorError(f"JFrog refused the console operator's exchange ({prov}): HTTP {res.status or res.error}")
            self._tok, self._exp = tok, self.clock() + float(data.get("expires_in") or 300)
            return tok

    def call(self, method: str, path: str, body=None):
        headers = {"Authorization": "Bearer " + self._token()}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        res = self.sh.http(method, f"https://{self.host}{path}", headers=headers, body=data, timeout=30)
        return res.status, (res.json() if res.body.strip()[:1] in ("{", "[") else None)
