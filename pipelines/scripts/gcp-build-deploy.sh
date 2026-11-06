#!/usr/bin/env bash
# Build and roll out ONE Cloud Run workload (its own stack in Setup). KIND picks how:
#   job       build apps/$APP (base image through JFrog), push to JFrog (JFROG_DOCKER_LOCAL, the
#             registry of record) and the same image to Artifact Registry (what Cloud Run pulls),
#             update the job and run it to completion
#   service   same image build, then update the service and check it is ready
#   worker-pool  same image build, then update the worker pool (gcloud beta: no GA group yet)
#   app-engine  deploy apps/$APP (App Engine standard, python312) from source as service $WORKLOAD,
#             running as the stack's service account; wheels vendored from JFrog as for function;
#             JFROG_* written into app.yaml env_variables (App Engine sets variables only on deploy)
#   function  deploy apps/$APP from source (gen2) with the jfrog_oidc client and its packages
#             vendored from JFrog (wheels/, so Google's build never reaches public PyPI);
#             before the stack has created it, upload the first package to its source bucket
# Every login comes from the service connection's OIDC token; nothing is stored.
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"
KIND=${KIND:-job}
: "${APP:?}" "${WORKLOAD:?}" "${GCP_REGION:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
loc=(--region "$GCP_REGION" --project "$GCP_PROJECT")

# An app with requirements.txt gets its wheels from JFrog (python 3.12, linux x86_64) with the
# OIDC token; its Dockerfile installs only from apps/<app>/wheels (never public PyPI).
fetch_wheels() {   # $1 = app dir
  [ -f "apps/$1/requirements.txt" ] && grep -q '/wheels' "apps/$1/Dockerfile" || return 0
  : "${JFROG_PYPI_REPO:?}"
  rm -rf "apps/$1/wheels"
  python3 -m jfrog_oidc pip --repo "$JFROG_PYPI_REPO" -- download --quiet --dest "apps/$1/wheels" \
    --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp \
    -r "apps/$1/requirements.txt"
}

build_image() {
  : "${IMAGE_REPOSITORY:?}" "${JFROG_DOCKER_REMOTE:?}" "${BASE_IMAGE:?}"
  # JFROG_DOCKER_LOCAL comes from gcp-jfrog-trust (variable group jfrog-oidc-gcp-<env>-jfrog).
  : "${JFROG_DOCKER_LOCAL:?run the gcp-jfrog-trust stack (JFrog OIDC) first: it creates the Docker repo}"
  local jfrog_host=${JFROG_URL#https://} registry="${GCP_REGION}-docker.pkg.dev" tag=${BUILD_SOURCEVERSION:0:12}
  # JFrog is the registry of record; Cloud Run pulls the same image from Artifact Registry with its
  # own identity (no JFrog password stored anywhere).
  jfrog_image="${jfrog_host}/${JFROG_DOCKER_LOCAL}/${WORKLOAD}:${tag}"
  image="${registry}/${GCP_PROJECT}/${IMAGE_REPOSITORY}/${WORKLOAD}:${tag}"
  python3 -m jfrog_oidc docker-login
  gcloud auth print-access-token | docker login --username oauth2accesstoken --password-stdin "https://${registry}"
  fetch_wheels "$APP"
  docker build --pull \
    --file "apps/${APP}/Dockerfile" \
    --build-arg APP_VERSION="$tag" \
    --build-arg BASE_IMAGE="${jfrog_host}/${JFROG_DOCKER_REMOTE}/${BASE_IMAGE}" \
    --label "org.opencontainers.image.revision=${BUILD_SOURCEVERSION}" \
    --tag "$jfrog_image" --tag "$image" .
  docker push "$jfrog_image"
  echo "published to JFrog: ${JFROG_DOCKER_LOCAL}/${WORKLOAD}:${tag}"
  docker push "$image"
}

# A new project has no job or service yet: Setup creates it from the first image. Push that image
# and say what to put in the stack's inputs, instead of failing on the update.
first_image() {
  echo "##vso[task.logissue type=warning]$1 $WORKLOAD does not exist yet: pushed $image. In Setup, set $2 to it and $3 on, deploy, then run this pipeline again."
  echo "##vso[build.addbuildtag]first-image"
  exit 0
}

case "$KIND" in
  job)
    build_image
    gcloud run jobs describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || first_image "Cloud Run job" job_image deploy_job
    gcloud run jobs update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    # Run it only once JFrog OIDC onboarding (its own step, later) has set what it reads.
    if gcloud run jobs describe "$WORKLOAD" "${loc[@]}" --format=json | grep -q '"JFROG_ARTIFACT_PATH"'; then
      gcloud run jobs execute "$WORKLOAD" "${loc[@]}" --wait --quiet
      echo "deployed and ran $image"
    else
      echo "deployed $image; not run: not onboarded to JFrog OIDC yet"
    fi
    ;;
  service)
    build_image
    gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || first_image "Cloud Run service" service_image deploy_service
    # Only the image changes: the identity and JFrog env the console wired stay.
    gcloud run services update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    ready=$(gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(status.conditions[0].status)')
    [ "$ready" = True ] || { echo "service $WORKLOAD is not ready" >&2; exit 1; }
    echo "deployed $image"
    ;;
  worker-pool)
    gcloud beta run worker-pools --help >/dev/null 2>&1 || { echo "gcloud on this agent has no 'beta run worker-pools'" >&2; exit 1; }
    build_image
    gcloud beta run worker-pools describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || first_image "Cloud Run worker pool" worker_pool_image deploy_worker_pool
    # Only the image changes: the identity and JFrog env the console wired stay.
    gcloud beta run worker-pools update "$WORKLOAD" --image "$image" "${loc[@]}" --quiet
    echo "deployed $image"
    ;;
  function)
    src=$(mktemp -d)
    cp "apps/${APP}/main.py" "$src/"
    cp -R client/src/jfrog_oidc "$src/"
    : "${JFROG_PYPI_REPO:?}"
    # Wheels for the function runtime (python312, linux x86_64), pulled from JFrog with the
    # OIDC token; requirements.txt then installs only from them.
    python3 -m jfrog_oidc pip --repo "$JFROG_PYPI_REPO" -- download --quiet --dest "$src/wheels" \
      --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp \
      -r "apps/${APP}/requirements.txt"
    { echo '--no-index'; echo '--find-links wheels'; grep -v '^\s*#' "apps/${APP}/requirements.txt"; } > "$src/requirements.txt"
    # Not created yet: the stack (gcp-cloud-run-function) creates it from a package in its source bucket.
    gcloud run services describe "$WORKLOAD" "${loc[@]}" --format='value(name)' >/dev/null 2>&1 || {
      object="${WORKLOAD}-${BUILD_SOURCEVERSION:0:12}.zip"
      (cd "$src" && find . -name __pycache__ -prune -o -type f -print | zip -q -@ "../$object")
      gcloud storage cp "$src/../$object" "gs://${GCP_PROJECT}-${WORKLOAD}-src/$object" --project "$GCP_PROJECT" --quiet
      echo "##vso[task.logissue type=warning]Cloud Run function $WORKLOAD does not exist yet: uploaded $object. In Setup, set source_object to it and deploy_function on, deploy, then run this pipeline again."
      echo "##vso[build.addbuildtag]first-image"
      exit 0
    }
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
  app-engine)
    # gcloud app deploy reads its source directory: the app, the client and wheels from JFrog.
    src=$(mktemp -d)/src
    mkdir -p "$src"
    cp -R "apps/${APP}/app" "apps/${APP}/gunicorn.conf.py" "$src/"
    cp -R client/src/jfrog_oidc "$src/"
    : "${JFROG_PYPI_REPO:?}"
    python3 -m jfrog_oidc pip --repo "$JFROG_PYPI_REPO" -- download --quiet --dest "$src/wheels" \
      --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp \
      -r "apps/${APP}/requirements.txt"
    { echo '--no-index'; echo '--find-links wheels'; grep -v '^\s*#' "apps/${APP}/requirements.txt"; } > "$src/requirements.txt"
    sa="${WORKLOAD}@${GCP_PROJECT}.iam.gserviceaccount.com"   # the stack's account (account_id = workload)
    gcloud iam service-accounts describe "$sa" --project "$GCP_PROJECT" --format='value(email)' >/dev/null 2>&1 || {
      echo "##vso[task.logissue type=error]Service account $sa does not exist: deploy the gcp-app-engine stack first." >&2; exit 1; }
    # JFROG_*: the serving version's values (what onboarding verified), else the defaults for this
    # environment. Values only: URL, provider, audience and source; never a token.
    env=$(gcloud app services describe "$WORKLOAD" --project "$GCP_PROJECT" --format=json 2>/dev/null | python3 -c '
import json, subprocess, sys
try:
    alloc = json.load(sys.stdin).get("split", {}).get("allocations", {})
except ValueError:
    alloc = {}
best = max(alloc.items(), key=lambda kv: kv[1], default=(None, 0))[0]
env = {}
if best:
    out = subprocess.run(["gcloud", "app", "versions", "describe", best, "--service", sys.argv[1], "--project", sys.argv[2],
                          "--format=json"], capture_output=True, text=True, timeout=120).stdout
    env = {k: v for k, v in (json.loads(out or "{}").get("envVariables") or {}).items() if k.startswith("JFROG_")}
print(json.dumps(env))' "$WORKLOAD" "$GCP_PROJECT")
    python3 - "$src/app.yaml" "$WORKLOAD" "$env" <<'PY'
import json, os, sys
path, service, carried = sys.argv[1], sys.argv[2], json.loads(sys.argv[3] or "{}")
env = {"JFROG_URL": os.environ["JFROG_URL"], "JFROG_OIDC_PROVIDER": os.environ.get("JFROG_RUNTIME_PROVIDER", "gcp-cloudrun"),
       "JFROG_OIDC_AUDIENCE": "jfrog-gcp", "JFROG_OIDC_SOURCE": "gcp", "JFROG_HEALTHCHECK_PATH": "", **carried}
env = {k: v for k, v in env.items() if v != ""}
base = open("apps/web-app/app-engine/app.yaml").read().rstrip("\n")
with open(path, "w") as f:
    f.write(base + f"\nservice: {json.dumps(service)}\nenv_variables:\n")
    f.writelines(f"  {k}: {json.dumps(v)}\n" for k, v in sorted(env.items()))
PY
    gcloud app deploy "$src/app.yaml" --project "$GCP_PROJECT" --service-account="$sa" \
      --version "v${BUILD_SOURCEVERSION:0:12}" --promote --quiet
    echo "deployed $WORKLOAD from ${BUILD_SOURCEVERSION:0:12}"
    ;;
  *) echo "unknown KIND: $KIND (job, service, worker-pool, function or app-engine)" >&2; exit 2 ;;
esac
