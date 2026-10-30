#!/usr/bin/env bash
# terraform-stack.sh <plan|apply> <stack> <environment> <plan-file>
#
# Runs terraform/stacks/<stack> with env/<environment>.tfvars and remote state.
# The JFrog provider gets an admin token from the setup connection's OIDC token
# (JFrog mapping ado-setup); it stays in this process's environment only.
set -euo pipefail
action=$1 stack=$2 environment=$3 plan_file=$4
cd "$BUILD_SOURCESDIRECTORY"
: "${TF_STATE_BUCKET:?}"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh

JFROG_ACCESS_TOKEN=$(python3 -m jfrog_oidc token)
export JFROG_ACCESS_TOKEN

cd "terraform/stacks/${stack}"
printf 'terraform {\n  backend "gcs" {}\n}\n' > backend_override.tf
terraform init -input=false -no-color \
  -backend-config="bucket=${TF_STATE_BUCKET}" \
  -backend-config="prefix=${stack}/${environment}"

case "$action" in
  plan)
    rc=0
    terraform plan -input=false -no-color -lock-timeout=5m \
      -var-file="env/${environment}.tfvars" -out="$plan_file" -detailed-exitcode || rc=$?
    [ "$rc" -eq 1 ] && exit 1
    echo "##vso[task.setvariable variable=hasChanges;isOutput=true]$([ "$rc" -eq 2 ] && echo true || echo false)"
    ;;
  apply)
    terraform apply -input=false -no-color -lock-timeout=5m "$plan_file"
    terraform output -no-color
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
