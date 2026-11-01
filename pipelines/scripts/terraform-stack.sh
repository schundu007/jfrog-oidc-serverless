#!/usr/bin/env bash
# terraform-stack.sh <plan|plan-destroy|apply> <stack> <environment> <plan-file>
#
# Runs terraform/stacks/<kind>/<cloud>/<stack> with the inputs the run was queued with (STACK_INPUTS,
# JSON: the stack's variables, as the Infrastructure page saves them) and the stack's
# remote state derived from STACK_STATE (JSON: gcp_project_id, or azure_subscription_id,
# ado_org and state_resource_group), the store that cloud's foundation stack creates. Nothing is read from
# files in the repo. Azure
# stacks authenticate through the AzureCLI@2 login; GCP stacks through WIF.
# Infrastructure stacks have no JFrog provider and get no JFrog token. A JFrog OIDC stack's run has
# the JFrog variable group (JFROG_OIDC_PROVIDER set): its JFrog admin token is the setup connection's
# OIDC token exchanged through the ado-setup mapping, in the environment only, never printed.
set -euo pipefail
action=$1 stack=$2 environment=$3 plan_file=$4
cd "$BUILD_SOURCESDIRECTORY"
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
if [ -n "${JFROG_OIDC_PROVIDER:-}" ]; then
  JFROG_ACCESS_TOKEN=$(python3 -m jfrog_oidc token)
  export JFROG_ACCESS_TOKEN
fi

inputs="$AGENT_TEMPDIRECTORY/${stack}.tfvars.json" state="$AGENT_TEMPDIRECTORY/${stack}.state.json"
printf '%s' "${STACK_INPUTS:-{\}}" > "$inputs"
printf '%s' "${STACK_STATE:-{\}}" > "$state"
python3 -m setup_console.stacks typed-inputs "$stack" "$inputs"   # ADO passes "False" for false
backend_args=$(python3 -m setup_console.stacks backend-args "$stack" "$environment" "$state")
mapfile -t backend <<< "$backend_args"
[ -n "$backend_args" ] || backend=()

cd "terraform/stacks/$(python3 -m setup_console.stacks dir "$stack")"
terraform init -input=false -no-color -reconfigure "${backend[@]}"

# JFrog OIDC objects the plan would create that already exist in JFrog (made before this stack
# owned them, or by a run that failed after creating them): "<address> <import id>" lines. The
# token is read from the environment, never passed in argv.
existing_jfrog() {
  terraform show -json "$plan_file" | python3 -c '
import json, os, sys, urllib.request, urllib.error
url, tok = os.environ["JFROG_URL"].rstrip("/"), os.environ["JFROG_ACCESS_TOKEN"]
def exists(path):
    req = urllib.request.Request(url + "/access/api/v1/oidc/" + path, headers={"Authorization": "Bearer " + tok})
    try:
        return urllib.request.urlopen(req, timeout=30).status == 200
    except urllib.error.HTTPError:
        return False
created = [c for c in json.load(sys.stdin).get("resource_changes", []) if c["change"]["actions"] == ["create"]]
# The integration before its mappings: a mapping is imported against its provider.
for c in created:
    a = c["change"]["after"] or {}
    if c["type"] == "platform_oidc_configuration" and exists(a["name"]):
        print(c["address"], a["name"])
for c in created:
    a = c["change"]["after"] or {}
    if c["type"] == "platform_oidc_identity_mapping" and exists(a["provider_name"] + "/identity_mappings/" + a["name"]):
        print(c["address"], a["name"] + ":" + a["provider_name"])
'
}

plan() {
  rc=0
  terraform plan -input=false -no-color -lock-timeout=5m $([ "$action" = plan-destroy ] && echo -destroy) \
    -var-file="$inputs" -out="$plan_file" -detailed-exitcode || rc=$?
  [ "$rc" -ne 1 ] || exit 1
}

case "$action" in
  plan|plan-destroy)
    plan
    if [ "$action" = plan ] && [ "$rc" -eq 2 ] && [ -n "${JFROG_ACCESS_TOKEN:-}" ]; then
      adopted=false
      while read -r addr id; do
        [ -n "$addr" ] || continue
        echo "$addr: already in JFrog, adopting it"
        terraform import -input=false -no-color -lock-timeout=5m -var-file="$inputs" "$addr" "$id" >/dev/null
        adopted=true
      done < <(existing_jfrog)
      [ "$adopted" = false ] || plan
    fi
    echo "##vso[task.setvariable variable=hasChanges;isOutput=true]$([ "$rc" -eq 2 ] && echo true || echo false)"
    ;;
  apply)
    terraform apply -input=false -no-color -lock-timeout=5m "$plan_file"
    terraform output -no-color
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
