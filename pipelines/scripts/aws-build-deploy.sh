#!/usr/bin/env bash
# aws-build-deploy.sh <lambda|ecs|apprunner> <workload> [ecs-cluster]
#
# Credentials: oidc-login.sh turns the build connection's OIDC token into
# AssumeRoleWithWebIdentity (AWS_ROLE_ARN = ado-build) and into the JFrog
# exchange (docker login to pull the base image through JFrog). The image goes
# to ECR, so the runtime pulls with its own role; JFrog serves the app's code
# at runtime through the workload's own identity (GetWebIdentityToken).
# Unverified live: see CLAUDE.md "Known unverified items" (AWS).
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"
target=${1:?usage: aws-build-deploy.sh <lambda|ecs|apprunner> <workload> [ecs-cluster]}
workload=${2:?workload name}
cluster=$(echo "${3:-}" | xargs)
: "${AWS_REGION:?}" "${AWS_ECR_REPOSITORY:?}" "${JFROG_DOCKER_REMOTE:?}" "${BASE_IMAGE:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
o=(--region "$AWS_REGION" --output json)
sha=${BUILD_SOURCEVERSION:0:12}

if [ "$target" = lambda ]; then
  pkg="$OIDC_DIR/pkg"
  mkdir -p "$pkg"
  cp apps/aws-lambda/handler.py "$pkg/"
  cp -r client/src/jfrog_oidc "$pkg/"
  (cd "$pkg" && find . -name '__pycache__' -prune -exec rm -rf {} + && zip -qr ../function.zip .)
  aws lambda update-function-code --function-name "$workload" --zip-file "fileb://$OIDC_DIR/function.zip" \
    "${o[@]}" --query '{version:Version,sha:CodeSha256}'
  aws lambda wait function-updated --function-name "$workload" --region "$AWS_REGION"
  aws lambda invoke --function-name "$workload" --cli-binary-format raw-in-base64-out \
    --payload '{"healthz": true}' "${o[@]}" "$OIDC_DIR/healthz.json" >/dev/null
  python3 - "$OIDC_DIR/healthz.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
body = json.loads(r.get("body") or "{}")
print(json.dumps({"status": r.get("statusCode"), "ok": body.get("ok"), "check": body.get("check")}))
sys.exit(0 if r.get("statusCode") == 200 else 1)
PY
  exit 0
fi

case "$target" in
  ecs) app=artifact-reader ;;
  apprunner) app=cloud-run-service ;;     # stdlib HTTP service with /healthz/jfrog
  *) echo "unknown target: $target" >&2; exit 2 ;;
esac
account=$(aws sts get-caller-identity --query Account --output text)
registry="${account}.dkr.ecr.${AWS_REGION}.amazonaws.com"
image="${registry}/${AWS_ECR_REPOSITORY}:${workload}-${sha}"

python3 -m jfrog_oidc docker-login
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"
docker build --pull --file "apps/${app}/Dockerfile" \
  --build-arg BASE_IMAGE="${JFROG_URL#https://}/${JFROG_DOCKER_REMOTE}/${BASE_IMAGE}" \
  --label "org.opencontainers.image.revision=${BUILD_SOURCEVERSION}" --tag "$image" .
docker push "$image"

if [ "$target" = ecs ]; then
  # A new revision of the family with the new image; every other setting (env the
  # console wired, task role) is carried over. The read-only fields are dropped.
  aws ecs describe-task-definition --task-definition "$workload" "${o[@]}" --query taskDefinition > "$OIDC_DIR/td.json"
  python3 - "$OIDC_DIR/td.json" "$image" <<'PY'
import json, sys
path, image = sys.argv[1], sys.argv[2]
td = json.load(open(path))
for k in ("taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
          "registeredAt", "registeredBy", "deregisteredAt"):
    td.pop(k, None)
td["containerDefinitions"][0]["image"] = image
json.dump(td, open(path, "w"))
PY
  aws ecs register-task-definition --cli-input-json "file://$OIDC_DIR/td.json" "${o[@]}" \
    --query 'taskDefinition.taskDefinitionArn'
  if [ -n "$cluster" ]; then
    aws ecs update-service --cluster "$cluster" --service "$workload" --task-definition "$workload" "${o[@]}" \
      --query 'service.deployments[0].{id:id,status:rolloutState}'
    aws ecs wait services-stable --cluster "$cluster" --services "$workload" --region "$AWS_REGION"
  fi
  exit 0
fi

arn=$(aws apprunner list-services "${o[@]}" --query "ServiceSummaryList[?ServiceName=='${workload}'].ServiceArn | [0]" --output text)
[ -n "$arn" ] && [ "$arn" != None ] || { echo "no App Runner service ${workload}" >&2; exit 1; }
# App Runner deploys the image the service names; with a fixed tag it is repointed
# through update-service, which keeps the env the console wired.
aws apprunner describe-service --service-arn "$arn" "${o[@]}" --query Service.SourceConfiguration > "$OIDC_DIR/src.json"
python3 - "$OIDC_DIR/src.json" "$image" <<'PY'
import json, sys
path, image = sys.argv[1], sys.argv[2]
src = json.load(open(path))
src["ImageRepository"]["ImageIdentifier"] = image
src.pop("CodeRepository", None)
json.dump(src, open(path, "w"))
PY
aws apprunner update-service --service-arn "$arn" --source-configuration "file://$OIDC_DIR/src.json" "${o[@]}" \
  --query 'Service.Status'
