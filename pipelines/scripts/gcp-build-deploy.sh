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

# A new project has no job or service yet: Setup creates it from the first image. Push that image
# and say what to put in the stack's inputs, instead of failing on the update.
first_image() {
  echo "##vso[task.logissue type=warning]$1 $WORKLOAD does not exist yet: pushed $image. In Setup, set job_image to it and deploy_job on, deploy, then run this pipeline again."
  echo "##vso[build.addbuildtag]first-image"
  exit 0
}

case "$KIND" in
  job)
    build_image
    gcloud run jobs describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || first_image "Cloud Run job"
    gcloud run jobs update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    gcloud run jobs execute "$WORKLOAD" "${loc[@]}" --wait --quiet
    echo "deployed and ran $image"
    ;;
  service)
    build_image
    gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || first_image "Cloud Run service"
    # Only the image changes: the identity and JFrog env the console wired stay.
    gcloud run services update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    ready=$(gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(status.conditions[0].status)')
    [ "$ready" = True ] || { echo "service $WORKLOAD is not ready" >&2; exit 1; }
    echo "deployed $image"
    ;;
  function)
    # Onboarding creates the function (identity + JFrog env); with none yet there is nothing to redeploy.
    gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || {
      echo "##vso[task.logissue type=warning]Cloud Run function $WORKLOAD is not deployed in ${GCP_PROJECT}: skipped."
      exit 0
    }
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
    # No --[no-]allow-unauthenticated: either flag rewrites the invoker policy
    # (run.services.setIamPolicy); a redeploy keeps the function's existing one.
    # A manual run and a CI run on the same commit deploy at once: the second gets 409
    # "unable to queue the operation" while the first is in flight. Wait and retry that only.
    for attempt in 1 2 3 4 5 6; do
      if out=$(gcloud functions deploy "$WORKLOAD" --gen2 --runtime python312 --source "$src" --entry-point handler \
        --trigger-http "${loc[@]}" "${args[@]}" --quiet 2>&1); then
        echo "$out"
        break
      fi
      echo "$out" >&2
      case "$out" in
        *"unable to queue the operation"*|*"status=[409]"*)
          [ "$attempt" -lt 6 ] || exit 1
          echo "another deploy of $WORKLOAD is in progress; retry $attempt of 5 in 30s" >&2
          sleep 30 ;;
        *) exit 1 ;;
      esac
    done
    echo "deployed $WORKLOAD from ${BUILD_SOURCEVERSION:0:12}"
    ;;
  *) echo "unknown KIND: $KIND (job, service or function)" >&2; exit 2 ;;
esac
