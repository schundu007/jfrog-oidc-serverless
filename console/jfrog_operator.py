"""The console operator's own short-lived JFrog token, by OIDC exchange (no stored token).

gcp-jfrog-trust's `console_operator_env` output names a Google service account, the audience and the
JFrog provider. The operator's gcloud login impersonates that account for an ID token (sensitive: never
logged or recorded) and exchanges it at JFrog. The JFrog token lives in memory only, cached per host until
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
STATE = pathlib.Path(__file__).resolve().parent.parent / "terraform/stacks/oidc/gcp/gcp-jfrog-trust/terraform.tfstate"


class OperatorError(RuntimeError):
    pass


def identity(state_file=STATE) -> dict:
    try:
        out = json.loads(pathlib.Path(state_file).read_text())["outputs"]["console_operator_env"]["value"] or {}
    except (OSError, ValueError, KeyError, TypeError):
        out = {}
    return {k: str(out.get(k) or "") for k in IDENTITY_KEYS}


class Operator:
    """JFrog REST calls as the console operator: call(method, path, body) -> (status, json or None)."""

    def __init__(self, shell, host: str, state_file=STATE, clock=time.time):
        self.sh, self.host, self.clock = shell, host, clock
        self.ident = identity(state_file)
        self._tok, self._exp = "", 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return all(self.ident.values())

    def _token(self) -> str:
        if not self.configured:
            raise OperatorError("no console operator identity: deploy gcp-jfrog-trust with console_operators")
        with self._lock:
            if self._tok and self._exp - REFRESH_BEFORE > self.clock():
                return self._tok
            sa, aud, prov = (self.ident["JFROG_OIDC_SERVICE_ACCOUNT"], self.ident["JFROG_OIDC_AUDIENCE"],
                             self.ident["JFROG_OIDC_PROVIDER"])
            r = self.sh.run(["gcloud", "auth", "print-identity-token", f"--impersonate-service-account={sa}",
                             f"--audiences={aud}", "--include-email"], timeout=60, sensitive=True)
            subject = r.stdout.strip() if r.ok else ""
            if not subject:
                raise OperatorError(f"gcloud could not get an ID token as {sa}")
            body = json.dumps({"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                               "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
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
