#!/usr/bin/env bash
# Build apps/$APP with its base image pulled through JFrog (OIDC token), push it
# to the workload's ACR, roll out the Container Apps job and run it to completion.
# Until the stack has created the job (deploy_job = true), it only pushes.
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"
: "${APP:?}" "${WORKLOAD:?}" "${JFROG_DOCKER_REMOTE:?}" "${BASE_IMAGE:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors >/dev/null

rg="rg-${WORKLOAD}"
job="caj-${WORKLOAD}"
acr=$(az acr list --resource-group "$rg" --query "[0].name" -o tsv)
[ -n "$acr" ] || { echo "no container registry in ${rg}; run azure-stack-setup first" >&2; exit 1; }
registry=$(az acr show --name "$acr" --query loginServer -o tsv)
image="${registry}/${WORKLOAD}:${BUILD_SOURCEVERSION:0:12}"

python3 -m jfrog_oidc docker-login
az acr login --name "$acr" --only-show-errors

docker build --pull \
  --file "apps/${APP}/Dockerfile" \
  --build-arg BASE_IMAGE="${JFROG_URL#https://}/${JFROG_DOCKER_REMOTE}/${BASE_IMAGE}" \
  --label "org.opencontainers.image.revision=${BUILD_SOURCEVERSION}" \
  --tag "$image" .
docker push "$image"

if ! az containerapp job show --name "$job" --resource-group "$rg" --only-show-errors >/dev/null 2>&1; then
  echo "pushed ${image}; ${job} is not deployed yet (deploy_job = true in the stack's env tfvars)"
  exit 0
fi

az containerapp job update --name "$job" --resource-group "$rg" --image "$image" --only-show-errors >/dev/null
execution=$(az containerapp job start --name "$job" --resource-group "$rg" --query name -o tsv --only-show-errors)
for _ in $(seq 1 60); do
  status=$(az containerapp job execution show --name "$job" --resource-group "$rg" \
    --job-execution-name "$execution" --query properties.status -o tsv --only-show-errors)
  case "$status" in
    Succeeded) echo "deployed and ran ${image} (${execution})"; exit 0 ;;
    Failed|Stopped|Degraded) echo "execution ${execution}: ${status}" >&2; exit 1 ;;
  esac
  sleep 10
done
echo "execution ${execution} did not finish in 10 minutes" >&2
exit 1
