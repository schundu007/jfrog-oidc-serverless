#!/usr/bin/env bash
# Build and roll out a Cloud Run workload. KIND picks how:
#   job       build apps/$APP (base image through JFrog), push to Artifact Registry,
#             update the job and run it to completion
#   service   same image build, then update the service and check it is ready
#   function  deploy apps/$APP from source (gen2) with the jfrog_oidc client in the package
# Every login comes from the service connection's OIDC token; nothing is stored.
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"
KIND=${KIND:-job}
: "${APP:?}" "${WORKLOAD:?}" "${GCP_REGION:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
loc=(--region "$GCP_REGION" --project "$GCP_PROJECT")

build_image() {
  : "${IMAGE_REPOSITORY:?}" "${JFROG_DOCKER_REMOTE:?}" "${BASE_IMAGE:?}"
  local jfrog_host=${JFROG_URL#https://} registry="${GCP_REGION}-docker.pkg.dev"
  image="${registry}/${GCP_PROJECT}/${IMAGE_REPOSITORY}/${WORKLOAD}:${BUILD_SOURCEVERSION:0:12}"
  python3 -m jfrog_oidc docker-login
  gcloud auth print-access-token | docker login --username oauth2accesstoken --password-stdin "https://${registry}"
  docker build --pull \
    --file "apps/${APP}/Dockerfile" \
    --build-arg BASE_IMAGE="${jfrog_host}/${JFROG_DOCKER_REMOTE}/${BASE_IMAGE}" \
    --label "org.opencontainers.image.revision=${BUILD_SOURCEVERSION}" \
    --tag "$image" .
  docker push "$image"
}

case "$KIND" in
  job)
    build_image
    gcloud run jobs update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    gcloud run jobs execute "$WORKLOAD" "${loc[@]}" --wait --quiet
    echo "deployed and ran $image"
    ;;
  service)
    build_image
    # Only the image changes: the identity and JFrog env the console wired stay.
    gcloud run services update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    ready=$(gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(status.conditions[0].status)')
    [ "$ready" = True ] || { echo "service $WORKLOAD is not ready" >&2; exit 1; }
    echo "deployed $image"
    ;;
  function)
    src=$(mktemp -d)
    cp "apps/${APP}/main.py" "$src/"
    cp -R client/src/jfrog_oidc "$src/"
    echo 'functions-framework==3.*' > "$src/requirements.txt"
    # A source deploy rewrites the function's config: carry over the runtime identity
    # and JFrog settings the console wired (on the underlying Cloud Run service).
    sa=$(gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(spec.template.spec.serviceAccountName)')
    env=$(gcloud run services describe "$WORKLOAD" "${loc[@]}" --format=json | python3 -c '
import json, sys
env = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env") or []
print("@".join(e["name"] + "=" + e["value"] for e in env if e["name"].startswith("JFROG_") and "value" in e))')
    args=()
    [ -n "$sa" ] && args+=(--service-account "$sa")
    [ -n "$env" ] && args+=("--update-env-vars=^@^$env")
    gcloud functions deploy "$WORKLOAD" --gen2 --runtime python312 --source "$src" --entry-point handler \
      --trigger-http --no-allow-unauthenticated "${loc[@]}" "${args[@]}" --quiet
    echo "deployed $WORKLOAD from ${BUILD_SOURCEVERSION:0:12}"
    ;;
  *) echo "unknown KIND: $KIND (job, service or function)" >&2; exit 2 ;;
esac
