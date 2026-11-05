# Sourced by the JFrog OIDC runs only: build-deploy, onboarding and the JFrog OIDC stacks'
# stack-setup (pipelines/oidc/...). Infrastructure runs source ado-login.sh alone and never
# touch JFrog.
#
# Signs in to the cloud (ado-login.sh), then makes the service connection's OIDC token the
# identity source for the JFrog exchange (jfrog_oidc, file source). JFROG_URL and
# JFROG_OIDC_PROVIDER come from the JFrog OIDC variable group jfrog-oidc-<cloud>-<env>-jfrog.

export ADO_SERVICE_CONNECTION_ID="${ADO_SERVICE_CONNECTION_ID:-${JFROG_OIDC_ADO_SERVICE_CONNECTION_ID:-}}"
# shellcheck source=pipelines/scripts/ado-login.sh
source "$BUILD_SOURCESDIRECTORY/pipelines/scripts/ado-login.sh"
export PYTHONPATH="$BUILD_SOURCESDIRECTORY/client/src${PYTHONPATH:+:$PYTHONPATH}"

export JFROG_OIDC_SOURCE=file
export JFROG_OIDC_TOKEN_FILE="$OIDC_DIR/id-token"
export JFROG_OIDC_AUDIENCE=api://AzureADTokenExchange

if [ -n "${JFROG_OIDC_PROVIDER:-}" ]; then
  : "${JFROG_URL:?}"
  python3 -m jfrog_oidc claims
  python3 -m jfrog_oidc check
fi
