#!/usr/bin/env bash
# terraform-stack.sh <plan|plan-destroy|apply> <stack> <environment> <plan-file>
#
# Runs terraform/stacks/<stack> with the inputs the run was queued with (STACK_INPUTS,
# JSON: the stack's variables, as the Infrastructure page saves them) and the stack's
# remote state derived from STACK_STATE (JSON: gcp_project_id, or azure_subscription_id,
# ado_org and state_resource_group), the store that cloud's foundation stack creates. Nothing is read from
# files in the repo. Azure
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

inputs="$AGENT_TEMPDIRECTORY/${stack}.tfvars.json" state="$AGENT_TEMPDIRECTORY/${stack}.state.json"
printf '%s' "${STACK_INPUTS:-{\}}" > "$inputs"
printf '%s' "${STACK_STATE:-{\}}" > "$state"
backend_args=$(python3 -m setup_console.stacks backend-args "$stack" "$environment" "$state")
mapfile -t backend <<< "$backend_args"
[ -n "$backend_args" ] || backend=()

cd "terraform/stacks/${stack}"
terraform init -input=false -no-color -reconfigure "${backend[@]}"

case "$action" in
  plan|plan-destroy)
    rc=0
    terraform plan -input=false -no-color -lock-timeout=5m $([ "$action" = plan-destroy ] && echo -destroy) \
      -var-file="$inputs" -out="$plan_file" -detailed-exitcode || rc=$?
    [ "$rc" -eq 1 ] && exit 1
    echo "##vso[task.setvariable variable=hasChanges;isOutput=true]$([ "$rc" -eq 2 ] && echo true || echo false)"
    ;;
  apply)
    terraform apply -input=false -no-color -lock-timeout=5m "$plan_file"
    terraform output -no-color
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
