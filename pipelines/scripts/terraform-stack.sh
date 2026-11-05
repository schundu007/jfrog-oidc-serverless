#!/usr/bin/env bash
# terraform-stack.sh <plan|plan-destroy|apply> <stack> <environment> <plan-file>
#
# Runs terraform/stacks/<serverless|kubernetes>/<cloud>/<stack> with the inputs the run was queued with (STACK_INPUTS,
# JSON: the stack's variables, as the Infrastructure page saves them) and the stack's
# remote state derived from STACK_STATE (JSON: gcp_project_id, or azure_subscription_id,
# ado_org and state_resource_group), the store that cloud's pipeline identity stack creates. Nothing is read from
# files in the repo. Azure
# stacks authenticate through the AzureCLI@2 login; GCP stacks through WIF.
# Infrastructure stacks only (terraform/stacks/{serverless,kubernetes}/...): no JFrog provider, token or
# variable. JFrog OIDC stacks run through oidc-stack.sh instead, never this script.
set -euo pipefail
action=$1 stack=$2 environment=$3 plan_file=$4
cd "$BUILD_SOURCESDIRECTORY"
# shellcheck source=pipelines/scripts/ado-login.sh
source pipelines/scripts/ado-login.sh

inputs="$AGENT_TEMPDIRECTORY/${stack}.tfvars.json" state="$AGENT_TEMPDIRECTORY/${stack}.state.json"
printf '%s' "${STACK_INPUTS:-{\}}" > "$inputs"
printf '%s' "${STACK_STATE:-{\}}" > "$state"
python3 -m setup_console.stacks typed-inputs "$stack" "$inputs"   # ADO passes "False" for false
backend_args=$(python3 -m setup_console.stacks backend-args "$stack" "$environment" "$state")
mapfile -t backend <<< "$backend_args"
[ -n "$backend_args" ] || backend=()

dir=$(python3 -m setup_console.stacks dir "$stack")
case "$dir" in serverless/*|kubernetes/*) ;; *) echo "$stack is not an infrastructure stack" >&2; exit 2 ;; esac
cd "terraform/stacks/$dir"
terraform init -input=false -no-color -reconfigure "${backend[@]}"

plan() {
  rc=0
  terraform plan -input=false -no-color -lock-timeout=5m $([ "$action" = plan-destroy ] && echo -destroy) \
    -var-file="$inputs" -out="$plan_file" -detailed-exitcode || rc=$?
  [ "$rc" -ne 1 ] || exit 1
}

case "$action" in
  plan|plan-destroy)
    plan
    echo "##vso[task.setvariable variable=hasChanges;isOutput=true]$([ "$rc" -eq 2 ] && echo true || echo false)"
    ;;
  apply)
    terraform apply -input=false -no-color -lock-timeout=5m "$plan_file"
    terraform output -no-color
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
