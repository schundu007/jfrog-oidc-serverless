#!/usr/bin/env bash
# Build apps/$APP with its base image pulled through JFrog (OIDC token), push it
# to Artifact Registry, roll out the Cloud Run job and run it to completion.
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"
: "${APP:?}" "${WORKLOAD:?}" "${GCP_REGION:?}" "${IMAGE_REPOSITORY:?}" "${JFROG_DOCKER_REMOTE:?}" "${BASE_IMAGE:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh

jfrog_host=${JFROG_URL#https://}
registry="${GCP_REGION}-docker.pkg.dev"
image="${registry}/${GCP_PROJECT}/${IMAGE_REPOSITORY}/${WORKLOAD}:${BUILD_SOURCEVERSION:0:12}"

python3 -m jfrog_oidc docker-login
gcloud auth print-access-token | docker login --username oauth2accesstoken --password-stdin "https://${registry}"

docker build --pull \
  --file "apps/${APP}/Dockerfile" \
  --build-arg BASE_IMAGE="${jfrog_host}/${JFROG_DOCKER_REMOTE}/${BASE_IMAGE}" \
  --label "org.opencontainers.image.revision=${BUILD_SOURCEVERSION}" \
  --tag "$image" .
docker push "$image"

gcloud run jobs update "$WORKLOAD" --image "$image" --region "$GCP_REGION" --quiet
gcloud run jobs execute "$WORKLOAD" --region "$GCP_REGION" --wait --quiet
echo "deployed and ran $image"
