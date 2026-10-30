"""Lambda function that reads from Artifactory with a short-lived token (stdlib only).

The function's execution role signs STS GetWebIdentityToken (outbound identity
federation); the JWT is exchanged for a JFrog access token in-process, once per
token lifetime per execution environment. Env, set by the console's Wire step:
    JFROG_URL, JFROG_OIDC_PROVIDER, JFROG_OIDC_AUDIENCE=jfrog-aws, JFROG_OIDC_SOURCE=aws,
    JFROG_OIDC_AWS_REGION
    JFROG_HEALTHCHECK_PATH  optional artifact for /healthz/jfrog to read

Package: zip handler.py with client/src/jfrog_oidc at the top level
(pipelines/scripts/aws-build-deploy.sh). Handler: handler.handler.
Invoked through a Function URL with AWS_IAM auth (payload 2.0) or directly:
    GET /healthz/jfrog       claims, exchange and read status (never the token)
    GET /?path=<repo/folder> files in that folder
A direct invoke with {"healthz": true} runs the self-test too.
"""

import json
import os

from jfrog_oidc import JFrogOidcError, JFrogTokenProvider, get_json
from jfrog_oidc.probe import selftest

_jfrog = None


def jfrog() -> JFrogTokenProvider:
    # Module scope survives between invocations of one execution environment.
    global _jfrog
    if _jfrog is None:
        _jfrog = JFrogTokenProvider.from_env()
    return _jfrog


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event, context):
    event = event or {}
    path = event.get("rawPath") or event.get("path") or ("/healthz/jfrog" if event.get("healthz") else "/")
    query = event.get("queryStringParameters") or {}
    if path == "/healthz/jfrog":
        result = selftest(os.environ.get("JFROG_HEALTHCHECK_PATH") or None, provider=jfrog())
        return _reply(200 if result["ok"] else 503, result)
    folder = query.get("path", "generic-local/models")
    try:
        listing = get_json(jfrog(), f"/artifactory/api/storage/{folder}")
    except (JFrogOidcError, ValueError) as exc:
        return _reply(502, {"error": str(exc)})
    files = [c["uri"].lstrip("/") for c in listing.get("children", []) if not c.get("folder")]
    return _reply(200, {"files": files, "token_user": jfrog().token().username})
