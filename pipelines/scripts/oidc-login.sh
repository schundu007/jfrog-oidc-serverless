# Sourced by the pipeline scripts. Azure DevOps issues a service connection's token
# only to a job that references the connection: the Azure pipelines run these scripts
# in AzureCLI@2, the GCP pipelines in a bash step after a skipped AzureCLI@2 reference.
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

# AWS: the same token assumes the connection's role (needs an aws-foundation stack; none today).
# The AWS CLI and the Terraform AWS provider read these two variables themselves
# (AssumeRoleWithWebIdentity); no keys are written anywhere.
if [ -n "${AWS_ROLE_ARN:-}" ]; then
  : "${AWS_REGION:?}"
  export AWS_WEB_IDENTITY_TOKEN_FILE="$OIDC_DIR/id-token"
  export AWS_ROLE_SESSION_NAME="ado-${BUILD_BUILDID:-local}"
  export AWS_CONFIG_FILE="$OIDC_DIR/aws-config" AWS_SHARED_CREDENTIALS_FILE="$OIDC_DIR/aws-credentials" AWS_PAGER=""
  aws sts get-caller-identity --query Arn --output text
fi
