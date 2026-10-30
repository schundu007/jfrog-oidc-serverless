"""Azure Function (Python v2 model) that reads from Artifactory with a short-lived token.

The same code runs on Flex Consumption, Premium/Dedicated (custom container) and
Functions on Container Apps; only the hosting differs.

App settings (set by terraform/examples/complete/azure.tf):
    JFROG_URL, JFROG_OIDC_PROVIDER=azure-entra, JFROG_OIDC_AUDIENCE=api://<audience-app-id>,
    JFROG_OIDC_SOURCE=azure, AZURE_CLIENT_ID=<user-assigned identity client ID>
    JFROG_HEALTHCHECK_PATH  optional artifact for /healthz/jfrog to read
"""

import json
import logging
import os

import azure.functions as func

from jfrog_oidc import JFrogOidcError, JFrogTokenProvider, get_json
from jfrog_oidc.probe import selftest

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

_jfrog = None


def jfrog() -> JFrogTokenProvider:
    # Lazy so a missing setting surfaces as a request error, not a host that
    # fails to index functions.
    global _jfrog
    if _jfrog is None:
        _jfrog = JFrogTokenProvider.from_env()
    return _jfrog


@app.route(route="artifacts", methods=["GET"])
def artifacts(req: func.HttpRequest) -> func.HttpResponse:
    folder = req.params.get("path", "generic-local/models")
    try:
        listing = get_json(jfrog(), f"/artifactory/api/storage/{folder}")
    except JFrogOidcError as exc:
        logging.error("JFrog token exchange failed: %s", exc)
        return func.HttpResponse("upstream auth failed", status_code=502)

    files = [c["uri"].lstrip("/") for c in listing.get("children", []) if not c.get("folder")]
    body = {"files": files, "token_user": jfrog().token().username}
    return func.HttpResponse(json.dumps(body), mimetype="application/json")


@app.route(route="healthz/jfrog", methods=["GET"])
def healthz_jfrog(req: func.HttpRequest) -> func.HttpResponse:
    # Claims, exchange and an artifact read; never the token. Protected by the
    # function key (http_auth_level above). The console calls this when a live
    # probe cannot borrow the app's identity (system-assigned identity).
    try:
        provider = jfrog()
    except ValueError as exc:
        return func.HttpResponse(json.dumps({"ok": False, "check": {"error": str(exc)}}), status_code=503,
                                 mimetype="application/json")
    result = selftest(os.environ.get("JFROG_HEALTHCHECK_PATH") or None, provider=provider)
    return func.HttpResponse(json.dumps(result), status_code=200 if result["ok"] else 503,
                             mimetype="application/json")
