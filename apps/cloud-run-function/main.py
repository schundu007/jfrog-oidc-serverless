"""Cloud Run function that reads from Artifactory with a short-lived token.

Env (set by terraform/stacks/all-targets/gcp.tf):
    JFROG_URL, JFROG_OIDC_PROVIDER=gcp-cloudrun, JFROG_OIDC_AUDIENCE=jfrog-gcp, JFROG_OIDC_SOURCE=gcp
    JFROG_HEALTHCHECK_PATH  optional artifact for /healthz/jfrog to read

GET /healthz/jfrog returns claims, exchange and read status (never the token).
Deploy without --allow-unauthenticated so Cloud Run IAM protects it.
"""

import os
import tempfile

import functions_framework

from jfrog_oidc import JFrogTokenProvider, download, get_json
from jfrog_oidc.probe import selftest

_jfrog = None


def jfrog() -> JFrogTokenProvider:
    # Created once per instance and reused across requests: one exchange per
    # token lifetime, refreshed 5 minutes before expiry.
    global _jfrog
    if _jfrog is None:
        _jfrog = JFrogTokenProvider.from_env()
    return _jfrog


@functions_framework.http
def handler(request):
    if request.path == "/healthz/jfrog":
        result = selftest(os.environ.get("JFROG_HEALTHCHECK_PATH") or None, provider=jfrog())
        return result, (200 if result["ok"] else 503)

    folder = request.args.get("path", "generic-local/models")
    listing = get_json(jfrog(), f"/artifactory/api/storage/{folder}")
    children = [c["uri"].lstrip("/") for c in listing.get("children", []) if not c.get("folder")]

    if request.args.get("fetch") and children:
        dest = os.path.join(tempfile.gettempdir(), children[0])
        download(jfrog(), f"{folder}/{children[0]}", dest)
        return {"downloaded": children[0], "bytes": os.path.getsize(dest)}

    return {"files": children, "token_user": jfrog().token().username}
