#!/usr/bin/env bash
# terraform-stack.sh <plan|plan-destroy|apply> <stack> <environment> <plan-file>
#
# Runs terraform/stacks/<stack> with env/<environment>.tfvars, and the stack's
# remote state (backend.tf) configured by env/<environment>.backend.hcl. Azure
# stacks authenticate through the AzureCLI@2 login; GCP stacks through WIF.
# The JFrog provider gets an admin token from the setup connection's OIDC token
# (JFrog mapping ado-setup); it stays in this process's environment only.
set -euo pipefail
action=$1 stack=$2 environment=$3 plan_file=$4
cd "$BUILD_SOURCESDIRECTORY"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh

JFROG_ACCESS_TOKEN=$(python3 -m jfrog_oidc token)
export JFROG_ACCESS_TOKEN

cd "terraform/stacks/${stack}"
terraform init -input=false -no-color -backend-config="env/${environment}.backend.hcl"

case "$action" in
  plan|plan-destroy)
    rc=0
    terraform plan -input=false -no-color -lock-timeout=5m $([ "$action" = plan-destroy ] && echo -destroy) \
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
