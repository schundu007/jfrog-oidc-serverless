#!/usr/bin/env bash
# oidc-stack.sh <plan|plan-destroy|apply> <stack> <environment> <plan-file>
#
# Runs the JFrog OIDC stack terraform/stacks/oidc/<cloud>/<stack> with the inputs the run was queued with (STACK_INPUTS,
# JSON: the stack's variables, as the Infrastructure page saves them) and the stack's
# remote state derived from STACK_STATE (JSON: gcp_project_id, or azure_subscription_id,
# ado_org and state_resource_group), the store that cloud's pipeline identity stack creates. Nothing is read from
# files in the repo. Azure
# stacks authenticate through the AzureCLI@2 login; GCP stacks through WIF.
# JFrog OIDC stacks only: infrastructure stacks run through terraform-stack.sh, never this script.
# The run has the JFrog variable group (JFROG_URL, JFROG_OIDC_PROVIDER): its JFrog admin token is the
# setup connection's OIDC token exchanged through the ado-setup mapping, in the environment only, never printed.
#
# azure-jfrog-trust (pipelines/oidc/azure/azure-jfrog-trust) makes that trust, so its first run has no
# variable group and no mapping yet: JFROG_ADMIN_TOKEN, a secret pipeline variable set for that run, is the
# token instead (delete it afterwards: later runs use the ado-setup mapping). JFROG_URL then comes from the
# inputs. The ADO organization, project, repository and tenant come from the run when left out, and its
# state lives in the pipeline identity's store (run-state.sh).
set -euo pipefail
action=$1 stack=$2 environment=$3 plan_file=$4
cd "$BUILD_SOURCESDIRECTORY"

inputs="$AGENT_TEMPDIRECTORY/${stack}.tfvars.json" state="$AGENT_TEMPDIRECTORY/${stack}.state.json"
printf '%s' "${STACK_INPUTS:-{\}}" > "$inputs"
printf '%s' "${STACK_STATE:-{\}}" > "$state"
dir=$(python3 -m setup_console.stacks dir "$stack")
case "$dir" in oidc/*) ;; *) echo "$stack is not a JFrog OIDC stack" >&2; exit 2 ;; esac
own_state=false
[ -f "terraform/stacks/$dir/backend.tf" ] || own_state=true
if [ "$own_state" = true ]; then
  read -r AZ_TENANT AZ_SUBSCRIPTION < <(az account show --query "join(' ', [tenantId, id])" -o tsv)
  export AZ_TENANT AZ_SUBSCRIPTION
  python3 -m setup_console.stacks run-inputs "$stack" "$environment" "$inputs"
fi
python3 -m setup_console.stacks typed-inputs "$stack" "$inputs"   # ADO passes "False" for false

admin=${JFROG_ADMIN_TOKEN:-}
unset JFROG_ADMIN_TOKEN
case "$admin" in '$('*) admin= ;; esac   # an undefined variable reaches the script as its own macro text
if [ "$own_state" = true ]; then
  JFROG_URL=${JFROG_URL:-$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("jfrog_url") or "")' "$inputs")}
  export JFROG_URL
  if [ -n "$admin" ]; then
    unset JFROG_OIDC_PROVIDER   # no trust yet: nothing to check
  else
    org=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["ado_org"])' "$inputs")
    export JFROG_OIDC_PROVIDER=${JFROG_OIDC_PROVIDER:-ado-$org-azure}
  fi
fi
# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
: "${JFROG_URL:?the JFrog OIDC variable group sets JFROG_URL}"
if [ -n "$admin" ]; then
  echo "JFrog: the run's JFROG_ADMIN_TOKEN (first run; delete the variable afterwards)"
  JFROG_ACCESS_TOKEN=$admin
else
  : "${JFROG_OIDC_PROVIDER:?the JFrog OIDC variable group sets JFROG_OIDC_PROVIDER}"
  JFROG_ACCESS_TOKEN=$(python3 -m jfrog_oidc token)
fi
unset admin
export JFROG_ACCESS_TOKEN

cd "terraform/stacks/$dir"
if [ "$own_state" = true ]; then
  # shellcheck source=pipelines/scripts/run-state.sh
  source "$BUILD_SOURCESDIRECTORY/pipelines/scripts/run-state.sh"
  run_state_init
else
  backend_args=$(cd "$BUILD_SOURCESDIRECTORY" && python3 -m setup_console.stacks backend-args "$stack" "$environment" "$state")
  mapfile -t backend <<< "$backend_args"
  [ -n "$backend_args" ] || backend=()
  terraform init -input=false -no-color -reconfigure "${backend[@]}"
fi

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
    if [ "$action" = plan ] && [ "$rc" -eq 2 ]; then
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
    [ "$own_state" = false ] || run_state_after_apply
    terraform output -no-color
    ;;
  *) echo "unknown action: $action" >&2; exit 2 ;;
esac
