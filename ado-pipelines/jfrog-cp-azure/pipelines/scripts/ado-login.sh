# Sourced by every pipeline script. Cloud sign-in only: nothing here knows about JFrog.
# Azure DevOps issues a service connection's token only to a job that references the
# connection: the pipelines run these scripts in AzureCLI@2, which signs in to Azure.
#
# The service connection's OIDC token (requested from System.OidcRequestUri with the job
# token: Entra-issued, aud api://AzureADTokenExchange, sub unique to the connection) is
# written to $OIDC_DIR/id-token.
# Everything lives in a private temp dir removed on exit. No token is printed.

: "${SYSTEM_ACCESSTOKEN:?map SYSTEM_ACCESSTOKEN: \$(System.AccessToken) on the step}"
: "${ADO_SERVICE_CONNECTION_ID:?map ADO_SERVICE_CONNECTION_ID on the step}"

umask 077
OIDC_DIR=$(mktemp -d)
trap 'rm -rf "$OIDC_DIR"' EXIT

python3 - "$OIDC_DIR/id-token" <<'PY'
import json, os, sys, urllib.parse, urllib.request
uri = os.environ["SYSTEM_OIDCREQUESTURI"]
if not uri.startswith("https://"):
    sys.exit("SYSTEM_OIDCREQUESTURI must be https")
q = urllib.parse.urlencode({"api-version": "7.1-preview.1", "serviceConnectionId": os.environ["ADO_SERVICE_CONNECTION_ID"]})
req = urllib.request.Request(f"{uri}{'&' if '?' in uri else '?'}{q}", data=b"{}", method="POST", headers={
    "Authorization": "Bearer " + os.environ["SYSTEM_ACCESSTOKEN"], "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    token = json.load(r).get("oidcToken")
if not isinstance(token, str) or token.count(".") != 2:
    sys.exit("Azure DevOps did not return the connection's OIDC token")
with open(sys.argv[1], "w", encoding="ascii") as f:
    f.write(token)
PY
unset SYSTEM_ACCESSTOKEN

export DOCKER_CONFIG="$OIDC_DIR/docker"
