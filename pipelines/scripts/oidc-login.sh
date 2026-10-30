# Sourced by the pipeline scripts.
#
# The service connection's OIDC token (requested from System.OidcRequestUri with
# the job token: Entra-issued, aud api://AzureADTokenExchange, sub unique to the
# connection) becomes:
#   - the identity source for the JFrog exchange (jfrog_oidc, file source)
#   - a gcloud / Google ADC login through Workload Identity Federation
# Everything lives in a private temp dir removed on exit. No token is printed.

: "${SYSTEM_ACCESSTOKEN:?map SYSTEM_ACCESSTOKEN: \$(System.AccessToken) on the step}"
: "${JFROG_OIDC_ADO_SERVICE_CONNECTION_ID:?}" "${JFROG_URL:?}" "${JFROG_OIDC_PROVIDER:?}"

umask 077
OIDC_DIR=$(mktemp -d)
trap 'rm -rf "$OIDC_DIR"' EXIT
export PYTHONPATH="$BUILD_SOURCESDIRECTORY/client/src${PYTHONPATH:+:$PYTHONPATH}"

python3 - "$OIDC_DIR/id-token" <<'PY'
import sys
from jfrog_oidc import AzureDevOpsSource
with open(sys.argv[1], "w", encoding="ascii") as f:
    f.write(AzureDevOpsSource().get_token("api://AzureADTokenExchange"))
PY
unset SYSTEM_ACCESSTOKEN

export JFROG_OIDC_SOURCE=file
export JFROG_OIDC_TOKEN_FILE="$OIDC_DIR/id-token"
export JFROG_OIDC_AUDIENCE=api://AzureADTokenExchange
export DOCKER_CONFIG="$OIDC_DIR/docker"

python3 -m jfrog_oidc claims
python3 -m jfrog_oidc check

if [ -n "${GCP_SERVICE_ACCOUNT:-}" ]; then
  : "${GCP_WIF_PROVIDER:?}" "${GCP_PROJECT:?}"
  export CLOUDSDK_CONFIG="$OIDC_DIR/gcloud"
  export GOOGLE_APPLICATION_CREDENTIALS="$OIDC_DIR/gcp-credentials.json"
  gcloud iam workload-identity-pools create-cred-config "$GCP_WIF_PROVIDER" \
    --service-account="$GCP_SERVICE_ACCOUNT" \
    --credential-source-file="$OIDC_DIR/id-token" \
    --output-file="$GOOGLE_APPLICATION_CREDENTIALS" --quiet
  gcloud auth login --cred-file="$GOOGLE_APPLICATION_CREDENTIALS" --quiet
  gcloud config set project "$GCP_PROJECT" --quiet
fi
