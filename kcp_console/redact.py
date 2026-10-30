"""Scrub secrets from anything the KCP console logs, stores, streams or returns.

Same approach as console/redact.py, with the KCP secrets: JFrog access and admin
tokens, JFROG_DOWNLOAD_TOKEN / JfrogDownloadToken, projected SA tokens and any
JWT, the Key Vault value artifactory-download-token, ADO PATs and Authorization
headers. Unlike the serverless redactor, a key named `value` is not masked
wholesale: kubelet config env items are `{name, value}` pairs whose values
(artifactory_url, provider names) are exactly what the console must show. Env
items whose *name* is secret-shaped are masked instead.
"""

from __future__ import annotations

import os
import re

MASK = "[REDACTED]"
ENV_SECRETS = ("JFROG_ACCESS_TOKEN", "JFROG_DOWNLOAD_TOKEN", "ARTIFACTORY_ADMIN_TOKEN", "AZURE_DEVOPS_EXT_PAT",
               "SYSTEM_ACCESSTOKEN", "JfrogDownloadToken")

# Keys whose values are always secrets, wherever they appear in a structure.
SECRET_KEYS = {
    "access_token", "accesstoken", "subject_token", "id_token", "refresh_token", "token", "password", "authorization",
    "client_secret", "jfrog_access_token", "jfrog_download_token", "jfrogdownloadtoken", "artifactory_admin_token",
    "artifactory-ado-agent-sa", "pat", "personal_access_token", "x-console-token", "secret_value",
}
SECRET_NAME = re.compile(r"(?i)token|password|secret|(^|_)pat$|^artifactory-ado-agent-sa$")

_SECRET_KEY_ALT = ("access_token|accessToken|subject_token|id_token|refresh_token|token|password|client_secret|"
                   "JFROG_ACCESS_TOKEN|JFROG_DOWNLOAD_TOKEN|JfrogDownloadToken|ARTIFACTORY_ADMIN_TOKEN|"
                   "AZURE_DEVOPS_EXT_PAT|SYSTEM_ACCESSTOKEN|artifactory-download-token")

# The "NAME: value" form only for named secret variables: generic words like "token:" appear in
# runbook prose and containerd messages ("failed to fetch anonymous token: unexpected status").
_SECRET_VAR_ALT = ("accessToken|client_secret|JFROG_ACCESS_TOKEN|JFROG_DOWNLOAD_TOKEN|JfrogDownloadToken|"
                   "ARTIFACTORY_ADMIN_TOKEN|AZURE_DEVOPS_EXT_PAT|SYSTEM_ACCESSTOKEN|artifactory-download-token")

_PATTERNS = [
    # JWTs: projected SA tokens, JFrog access tokens, Azure AD tokens
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), MASK),
    # JFrog reference tokens: base64 of "reftkn:01:"
    (re.compile(r"cmVmdGtuOjAxOj[A-Za-z0-9+/=_-]+"), MASK),
    # Authorization: Bearer|Basic <value>
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)?\s*)[^\s,'\"}]+"), r"\1" + MASK),
    # (but not a WWW-Authenticate challenge: `Bearer realm="...",service="..."` carries no credential)
    (re.compile(r"(?i)\b(bearer\s+)(?!\[REDACTED\])(?!realm=)[A-Za-z0-9._~+/=:-]{6,}"), r"\1" + MASK),
    # curl -u user:secret and user:secret@host
    (re.compile(r"(-u\s+[^:\s]+:)\S+"), r"\1" + MASK),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),
    # X-JFrog-Art-Api / X-Console-Token headers
    (re.compile(r"(?i)\b((?:x-jfrog-art-api|x-console-token)[\"']?\s*[:=]\s*[\"']?)[^\s,'\"}]+"), r"\1" + MASK),
    # NAME=value, --set a.b.accessToken=value, $(NAME) already expanded, "NAME": "value"
    # (=: any value; ": " only a token-shaped one, so prose like "ADMIN_TOKEN: it was typed" survives)
    (re.compile(r"(\b(?:%s)\s*=\s*)(?!\*\*\*)(?!\[REDACTED\])(?!\$)[^\s,'\"}&]+" % _SECRET_KEY_ALT), r"\1" + MASK),
    (re.compile(r"(\b(?:%s)\s*:\s*)(?!\*\*\*)(?!\[REDACTED\])(?!\$)(?=[^\s,'\"}&]{8,})[^\s,'\"}&]+" % _SECRET_VAR_ALT),
     r"\1" + MASK),
    (re.compile(r'("(?:%s)"\s*:\s*")[^"]*(")' % _SECRET_KEY_ALT, re.I), r"\1" + MASK + r"\2"),
    # query-string secrets and the console's own #t=<session token>
    (re.compile(r"(?i)([?&;#](?:access_token|token|password|t)=)[^&#\s\"']+"), r"\1" + MASK),
    # Azure DevOps PATs: 52 chars of base32 lowercase, or the 84-char form
    (re.compile(r"\b[a-z2-7]{52}\b"), MASK),
    (re.compile(r"\b[A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4}\b"), MASK),
]


def _env_secrets() -> list:
    return [v for v in (os.environ.get(n, "") for n in ENV_SECRETS) if len(v) >= 8]


def text(s):
    if not isinstance(s, str) or not s:
        return s
    for secret in _env_secrets():
        s = s.replace(secret, MASK)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def obj(value):
    """Redact a JSON-like structure. Secret-named keys are masked whole, and so is the
    `value` of a {name, value} item whose name is secret-shaped (env lists, pipeline variables)."""
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        named_secret = isinstance(value.get("name"), str) and bool(SECRET_NAME.search(value["name"]))
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, str) and v and (
                    k.lower() in SECRET_KEYS or (named_secret and k == "value")):
                out[k] = MASK
            else:
                out[k] = obj(v)
        return out
    if isinstance(value, (list, tuple)):
        return [obj(v) for v in value]
    return value


def argv(args: list) -> list:
    return [text(a) for a in args]
