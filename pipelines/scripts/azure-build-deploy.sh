#!/usr/bin/env bash
# Build and roll out ONE Azure serverless workload. KIND picks the type; each type is its own
# stack (Setup) with its own resource group rg-$WORKLOAD, so a run never touches another workload:
#   job                Container Apps job: image (artifact-reader) -> its ACR, update, run to completion
#   app                Container App: image (cloud-run-service) -> its ACR, update, check it runs
#   functions-aca      Functions on Container Apps: image (azure-function) -> its ACR, update
#   functions-premium  Premium / Dedicated function app: code package (azure-function), zip deploy
#   functions-flex     Flex Consumption function app: code package (azure-function), zip deploy
#                      (the platform keeps it in the app's own storage, read with its managed identity)
#   webapp             App Service web app, Linux or Windows: code (apps/web-app/<runtime>) built
#                      with pip, Maven or dotnet through JFrog only, then zip/jar deploy. The app's
#                      jfrog-oidc-runtime/-version tags (set by the stack) pick the build.
# Base images come through JFrog (OIDC token); every login comes from the service connection's
# OIDC token, nothing is stored. Until the stack has created the workload, an image kind only
# pushes and says what to set in Setup.
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"
KIND=${KIND:-job}
: "${WORKLOAD:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors >/dev/null

rg="rg-${WORKLOAD}"
az group show --name "$rg" --only-show-errors >/dev/null 2>&1 || {
  echo "##vso[task.logissue type=warning]${WORKLOAD} is not deployed (no resource group ${rg}): skipped. Deploy its stack in Setup first."
  exit 0
}

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

build_image() {   # $1 = app dir, $2 = base image
  : "${JFROG_DOCKER_REMOTE:?}"
  acr=$(az acr list --resource-group "$rg" --query "[0].name" -o tsv)
  [ -n "$acr" ] || { echo "no registry in ${rg}" >&2; exit 1; }
  registry=$(az acr show --name "$acr" --query loginServer -o tsv)
  repo_tag="${WORKLOAD}:${BUILD_SOURCEVERSION:0:12}"
  image="${registry}/${repo_tag}"
  python3 -m jfrog_oidc docker-login
  az acr login --name "$acr" --only-show-errors
  fetch_wheels "$1"
  docker build --pull --file "apps/$1/Dockerfile" --build-arg BASE_IMAGE="$2" \
    --build-arg APP_VERSION="${BUILD_SOURCEVERSION:0:12}" \
    --label "org.opencontainers.image.revision=${BUILD_SOURCEVERSION}" --tag "$image" .
  docker push "$image"
}

first_image() {   # $1 = what, $2 = image input, $3 = switch input
  echo "##vso[task.logissue type=warning]$1 does not exist yet: pushed ${repo_tag}. In Setup, set $2 to it and $3 on, deploy, then run this pipeline again."
  echo "##vso[build.addbuildtag]first-image"
  exit 0
}

via_jfrog() { echo "${JFROG_URL#https://}/${JFROG_DOCKER_REMOTE}/$1"; }

zip_deploy() {
  app=$(az functionapp list --resource-group "$rg" --query "[0].name" -o tsv)
  [ -n "$app" ] || { echo "no function app in ${rg}" >&2; exit 1; }
  src=$(mktemp -d)
  cp apps/azure-function/function_app.py apps/azure-function/host.json "$src/"
  cp -R client/src/jfrog_oidc "$src/"
  : > "$src/requirements.txt"   # azure.functions comes with the Python worker; the client is stdlib only
  chmod -R a+rX "$src"   # the Functions host runs as a non-root user: every file must be readable
  (cd "$src" && find . -name __pycache__ -prune -o -type f -print | zip -q -@ ../package.zip)
  az functionapp deployment source config-zip --name "$app" --resource-group "$rg" \
    --src "$src/../package.zip" --build-remote false --only-show-errors >/dev/null
  echo "deployed ${BUILD_SOURCEVERSION:0:12} to ${app}"
}

case "$KIND" in
  job)
    build_image artifact-reader "$(via_jfrog "${BASE_IMAGE:-library/python:3.12-slim}")"
    job="caj-${WORKLOAD}"
    az containerapp job show --name "$job" --resource-group "$rg" --only-show-errors >/dev/null 2>&1 \
      || first_image "Container Apps job ${job}" job_image deploy_job
    az containerapp job update --name "$job" --resource-group "$rg" --image "$image" --only-show-errors >/dev/null
    # Run it only once JFrog OIDC onboarding (its own step, later) has set what it reads.
    az containerapp job show --name "$job" --resource-group "$rg" -o json --only-show-errors | grep -q '"JFROG_ARTIFACT_PATH"' || {
      echo "deployed ${image}; not run: not onboarded to JFrog OIDC yet"; exit 0; }
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
    ;;
  app|functions-aca)
    if [ "$KIND" = app ]; then
      build_image cloud-run-service "$(via_jfrog "${BASE_IMAGE:-library/python:3.12-slim}")"
      what="Container App ${WORKLOAD}" input=app_image switch=deploy_app
    else
      build_image azure-function "${FUNCTIONS_BASE_IMAGE:-mcr.microsoft.com/azure-functions/python:4-python3.12}"
      what="Functions on Container Apps ${WORKLOAD}" input=function_image switch=deploy_function
    fi
    az containerapp show --name "$WORKLOAD" --resource-group "$rg" --only-show-errors >/dev/null 2>&1 \
      || first_image "$what" "$input" "$switch"
    # Only the image changes: the identity and JFrog env the console wired stay.
    az containerapp update --name "$WORKLOAD" --resource-group "$rg" --image "$image" --only-show-errors >/dev/null
    state=$(az containerapp show --name "$WORKLOAD" --resource-group "$rg" --query properties.runningStatus -o tsv --only-show-errors)
    echo "deployed ${image} (${state:-unknown})"
    ;;
  functions-premium)
    zip_deploy
    ;;
  functions-flex)
    # The provider writes an account-key AzureWebJobsStorage on Flex; shared keys are off, so the host
    # would not start. Drop it: the host uses AzureWebJobsStorage__* with its managed identity.
    app=$(az functionapp list --resource-group "$rg" --query "[0].name" -o tsv)
    [ -n "$app" ] || { echo "no function app in ${rg}" >&2; exit 1; }
    keyed=$(az functionapp config appsettings list --name "$app" --resource-group "$rg" \
      --query "[?name=='AzureWebJobsStorage'].name" -o tsv --only-show-errors)
    if [ -n "$keyed" ]; then
      az functionapp config appsettings delete --name "$app" --resource-group "$rg" \
        --setting-names AzureWebJobsStorage --only-show-errors >/dev/null
      echo "host storage: managed identity only (removed the account-key setting)"
    fi
    zip_deploy
    ;;
  webapp)
    az webapp show --name "$WORKLOAD" --resource-group "$rg" --only-show-errors >/dev/null 2>&1 || {
      echo "no web app ${WORKLOAD} in ${rg}" >&2; exit 1; }
    runtime=$(az webapp show --name "$WORKLOAD" --resource-group "$rg" --query 'tags."jfrog-oidc-runtime"' -o tsv --only-show-errors)
    version=$(az webapp show --name "$WORKLOAD" --resource-group "$rg" --query 'tags."jfrog-oidc-version"' -o tsv --only-show-errors)
    # Python is the same JFrog browser as the Cloud Run / Container Apps service.
    case "$runtime" in python) src=apps/cloud-run-service ;; *) src="apps/web-app/${runtime}" ;; esac
    [ -d "$src" ] || { echo "web app ${WORKLOAD} has runtime '${runtime}': no ${src}" >&2; exit 1; }
    out=$(mktemp -d)
    case "$runtime" in
      python)
        : "${JFROG_PYPI_REPO:?}"
        cp -R "$src/app" "$src/gunicorn.conf.py" "$src/requirements.txt" "$out/"
        cp -R client/src/jfrog_oidc "$out/"
        # Linux App Service only: wheels for its python, installed into the package (PYTHONPATH).
        python3 -m jfrog_oidc pip --repo "$JFROG_PYPI_REPO" -- install --quiet --target "$out/.python_packages" \
          --only-binary=:all: --platform manylinux2014_x86_64 --python-version "${version:-3.12}" \
          --implementation cp -r "$src/requirements.txt"
        # The app's tests, against the packages being shipped.
        (cd "$src" && PYTHONPATH="$out/.python_packages:$out" APP_JSON_LOGS=0 python3 -m unittest discover -s tests -t . -q)
        (cd "$out" && find . -name __pycache__ -prune -o -type f -print | zip -q -@ ../package.zip)
        pkg="$out/../package.zip" type=zip
        ;;
      java)
        : "${JFROG_MAVEN_REPO:?}"
        jdk_var="JAVA_HOME_${version:-21}_X64"
        export JAVA_HOME="${!jdk_var:-${JAVA_HOME:-}}"
        python3 -m jfrog_oidc mvn --repo "$JFROG_MAVEN_REPO" -- -B -q -f "$src/pom.xml" \
          -Dmaven.repo.local="$out/m2" -Dapp.version="${BUILD_SOURCEVERSION:0:12}" package
        pkg="$src/target/app.jar" type=jar
        ;;
      dotnet)
        : "${JFROG_NUGET_REPO:?}"
        export NUGET_PACKAGES="$out/nuget" DOTNET_CLI_TELEMETRY_OPTOUT=1
        fw="net${version:-8.0}"
        # Restores the tests too (they reference the app); packages only from JFrog.
        python3 -m jfrog_oidc dotnet --repo "$JFROG_NUGET_REPO" -- restore "$src.tests" -p:AppFramework="$fw"
        dotnet test "$src.tests" --no-restore -p:AppFramework="$fw"
        dotnet publish "$src" --configuration Release --no-restore --output "$out/publish" \
          -p:AppFramework="$fw" -p:SourceRevisionId="${BUILD_SOURCEVERSION:0:12}"
        (cd "$out/publish" && zip -q -r ../package.zip .)
        pkg="$out/package.zip" type=zip
        ;;
      *) echo "web app ${WORKLOAD}: unknown runtime '${runtime}' (python, java or dotnet)" >&2; exit 1 ;;
    esac
    # Basic-auth publishing is off on the app: az deploys with the service connection's Entra token.
    started=$(date -u +%Y-%m-%dT%H:%M:%S)
    if ! az webapp deploy --name "$WORKLOAD" --resource-group "$rg" --src-path "$pkg" --type "$type" \
      --restart true --only-show-errors >/dev/null; then
      # az's status poll can fail (Kudu 502 while the site restarts) after the deploy itself
      # succeeded: Kudu's own record of the newest deployment decides (status 4 = success).
      deployed=""
      for _ in $(seq 1 40); do
        last=$(az webapp log deployment list --name "$WORKLOAD" --resource-group "$rg" \
          --query "[0].[received_time, to_string(complete), to_string(status)]" -o tsv 2>/dev/null | tr '\n' ' ' || true)
        read -r at done status <<<"$last"
        if [[ -n "$at" && "$at" > "$started" && "$done" == "true" ]]; then
          [[ "$status" == "4" ]] && deployed=1
          break
        fi
        sleep 15
      done
      [ -n "$deployed" ] || { echo "web app ${WORKLOAD}: deployment failed" >&2; exit 1; }
      echo "the deploy call failed, but the newest deployment on ${WORKLOAD} completed successfully"
    fi
    echo "deployed ${BUILD_SOURCEVERSION:0:12} (${runtime} ${version}) to ${WORKLOAD}"
    ;;
  *) echo "unknown KIND: $KIND (job, app, functions-aca, functions-premium, functions-flex or webapp)" >&2; exit 2 ;;
esac
