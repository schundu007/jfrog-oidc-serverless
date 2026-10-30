"""Scrub tokens and secrets from anything that is logged, stored or streamed.

Every log line, SSE payload and stored evidence goes through text() or obj().
Masks JWT-shaped strings, JFrog reference tokens, Authorization/Bearer values,
Basic-auth URLs, keyvaultref values, --password* argv values, secret-named JSON
fields and query parameters (incl. Functions ?code= keys and SAS sig=),
secret-bearing headers, and the current JFROG_ACCESS_TOKEN value.
"""

from __future__ import annotations

import os
import re

MASK = "[REDACTED]"

# Keys whose values are always secrets, wherever they appear in a structure.
SECRET_KEYS = {
    "access_token", "subject_token", "id_token", "refresh_token", "token", "password",
    "value", "identity_header", "x-identity-header", "jfrog_access_token", "authorization",
    "client_secret", "function_key", "key",
    # AWS credentials and STS answers (container credentials endpoint, get-caller-identity never has these)
    "secretaccesskey", "sessiontoken", "webidentitytoken", "aws_secret_access_key", "aws_session_token",
}

_PATTERNS = [
    # JWTs (identity tokens, JFrog access tokens)
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), MASK),
    # Google OAuth access tokens (gcloud auth print-access-token, metadata server)
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}"), MASK),
    # JFrog reference tokens: base64 of "reftkn:01:"
    (re.compile(r"cmVmdGtuOjAxOj[A-Za-z0-9+/=_-]+"), MASK),
    # Authorization: Bearer|Basic <value>
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)?\s*)[^\s,'\"}]+"), r"\1" + MASK),
    # bare "Bearer <value>"
    (re.compile(r"(?i)\b(bearer\s+)(?!\[REDACTED\])[A-Za-z0-9._~+/=:-]{6,}"), r"\1" + MASK),
    # user:secret@host in URLs
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),
    # Container Apps keyvaultref:<url>
    (re.compile(r"(keyvaultref:)[^,\s'\"]+"), r"\1" + MASK),
    # --password <v> / --password=<v> / --password-foo <v>, but not --password-stdin
    (re.compile(r"(--password(?!-stdin)[\w-]*)(=|\s+)(?!-)(\S+)"), r"\1\2" + MASK),
    # secret-named query/form parameters, incl. Azure Functions ?code= keys, SAS sig=,
    # and the console's own #t=<session token> fragment
    (re.compile(r"(?i)([?&;#](?:access_token|subject_token|id_token|refresh_token|token|password|client_secret"
                r"|code|sig|t)=)[^&#\s\"']+"), r"\1" + MASK),
    # secret-bearing headers written as "Name: value"
    (re.compile(r"(?i)\b((?:x-functions-key|x-identity-header|x-console-token|x-jfrog-art-api)[\"']?\s*[:=]\s*[\"']?)"
                r"[^\s,'\"}]+"), r"\1" + MASK),
    # AWS: secret keys / session tokens in JSON or ini form, presigned and signed-request values
    (re.compile(r'(?i)("(?:SecretAccessKey|SessionToken|WebIdentityToken|Token)"\s*:\s*")[^"]*(")'), r"\1" + MASK + r"\2"),
    (re.compile(r"(?i)\b(aws_secret_access_key|aws_session_token)(\s*[=:]\s*)\S+"), r"\1\2" + MASK),
    (re.compile(r"(?i)([?&](?:X-Amz-Signature|X-Amz-Security-Token|X-Amz-Credential)=)[^&#\s\"']+"), r"\1" + MASK),
    (re.compile(r"(?i)\b(x-amz-security-token[\"']?\s*[:=]\s*[\"']?)[^\s,'\"}]+"), r"\1" + MASK),
    (re.compile(r"(<(?:WebIdentityToken|SessionToken|SecretAccessKey)>)[^<]*(</)"), r"\1" + MASK + r"\2"),
    # "secret_field": "value" inside serialised JSON
    (re.compile(r'("(?:access_token|subject_token|id_token|refresh_token|password|value|token|client_secret)"\s*:\s*")[^"]*(")'),
     r"\1" + MASK + r"\2"),
]


def _env_secrets() -> list:
    vals = []
    for name in ("JFROG_ACCESS_TOKEN",):
        v = os.environ.get(name, "")
        if len(v) >= 8:
            vals.append(v)
    return vals


def text(s: str) -> str:
    if not isinstance(s, str) or not s:
        return s
    for secret in _env_secrets():
        s = s.replace(secret, MASK)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def obj(value):
    """Redact a JSON-like structure recursively; secret-named keys are masked whole."""
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in SECRET_KEYS and isinstance(v, str) and v:
                out[k] = MASK
            else:
                out[k] = obj(v)
        return out
    if isinstance(value, (list, tuple)):
        return [obj(v) for v in value]
    return value


def argv(args: list) -> list:
    """Redact an argv list: the value after --password* and inline --password*=value."""
    out = []
    mask_next = False
    for a in args:
        if mask_next:
            out.append(MASK)
            mask_next = False
            continue
        if a.startswith("--password") and a != "--password-stdin":
            if "=" in a:
                out.append(a.split("=", 1)[0] + "=" + MASK)
            else:
                out.append(a)
                mask_next = True
            continue
        out.append(text(a))
    return out
