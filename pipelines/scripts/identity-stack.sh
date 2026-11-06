#!/usr/bin/env bash
# identity-stack.sh <plan|plan-destroy|apply> <environment> <plan-file>
#
# terraform/stacks/serverless/azure/azure-pipeline-identity from its ADO pipeline
# (pipelines/serverless/azure/azure-pipeline-identity/stack-setup.yml). Runs in AzureCLI@2 signed in
# through the bootstrap service connection: created once in the ADO project, before
# this stack creates the pipelines' own (jfrog-oidc-azure-build/-setup). Its identity needs Owner on the
# subscription, Entra rights to grant app roles, and Project Administrator in the ADO project.
#
# STACK_INPUTS: the stack's variables as JSON. The ADO organization and project, the tenant and the
# subscription come from the run itself when left out. State: run-state.sh (the store this stack creates).
# Infrastructure only: no JFrog provider, token or variable.
set -euo pipefail
action=$1 environment=$2 plan_file=$3 stack=azure-pipeline-identity
cd "$BUILD_SOURCESDIRECTORY"

inputs="$AGENT_TEMPDIRECTORY/${stack}.tfvars.json"
printf '%s' "${STACK_INPUTS:-{\}}" > "$inputs"
read -r AZ_TENANT AZ_SUBSCRIPTION < <(az account show --query "join(' ', [tenantId, id])" -o tsv)
export AZ_TENANT AZ_SUBSCRIPTION
python3 -m setup_console.stacks run-inputs "$stack" "$environment" "$inputs"
python3 -m setup_console.stacks typed-inputs "$stack" "$inputs"   # ADO passes "False" for false

cd "terraform/stacks/$(python3 -m setup_console.stacks dir "$stack")"
# shellcheck source=pipelines/scripts/run-state.sh
source "$BUILD_SOURCESDIRECTORY/pipelines/scripts/run-state.sh"
run_state_init

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
    run_state_after_apply
    terraform output -no-color
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
