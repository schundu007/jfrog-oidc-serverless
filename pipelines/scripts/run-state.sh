# Sourced by oidc-stack.sh when ADO runs a stack that has no backend.tf
# (azure-pipeline-identity, azure-jfrog-trust). Its state lives in the pipeline identity's storage account,
# key <stack>/<env>.tfstate, through a backend_override.tf.json written on the agent only.
#
# Before that store exists (the pipeline identity's first apply), the run keeps state on the agent and
# moves it into the store right after the apply. Destroying the pipeline identity removes the store too:
# that run plans and applies against a copy of the state carried in the plan artifact.
#
# Needs: stack, environment, inputs, plan_file set; the current directory is the stack's.

run_state_backend() {
  mapfile -t backend < <(cd "$BUILD_SOURCESDIRECTORY" && python3 -m setup_console.stacks run-backend "$stack" "$environment" "$inputs")
  [ "${#backend[@]}" -gt 0 ] || { echo "no state location for $stack" >&2; exit 1; }
  local kv
  for kv in "${backend[@]}"; do
    case "$kv" in
      -backend-config=storage_account_name=*) state_account=${kv#-backend-config=storage_account_name=} ;;
      -backend-config=resource_group_name=*) state_rg=${kv#-backend-config=resource_group_name=} ;;
    esac
  done
}

remote_backend() { printf '%s' '{"terraform":{"backend":{"azurerm":{}}}}' > backend_override.tf.json; }

# Sets STATE_MODE: remote (in the store), local (no store yet) or copy (a copy from the plan artifact).
run_state_init() {
  run_state_backend
  local copy
  copy="$(dirname "$plan_file")/terraform.tfstate"
  if [ "$action" = apply ] && [ -f "$copy" ]; then
    STATE_MODE=copy
    rm -f backend_override.tf.json
    cp "$copy" terraform.tfstate
    terraform init -input=false -no-color -reconfigure
  elif az storage account show -n "$state_account" -g "$state_rg" -o none 2>/dev/null; then
    STATE_MODE=remote
    remote_backend
    terraform init -input=false -no-color -reconfigure "${backend[@]}"
    if [ "$action" = plan-destroy ] && [ "$stack" = azure-pipeline-identity ]; then
      # The destroy removes the store itself: plan against a copy, carried to the apply in the plan artifact.
      terraform state pull > "$copy"
      rm -f backend_override.tf.json
      cp "$copy" terraform.tfstate
      terraform init -input=false -no-color -reconfigure >/dev/null
      STATE_MODE=copy
    fi
  else
    STATE_MODE=local
    echo "The state store $state_account does not exist yet: state stays on the agent until the apply creates it."
    rm -f backend_override.tf.json
    terraform init -input=false -no-color -reconfigure
  fi
}

# After a first apply: move the state into the store the apply just made. Data access (role assignment)
# can take a few minutes to reach the storage service.
run_state_after_apply() {
  [ "$STATE_MODE" = local ] || return 0
  [ -s terraform.tfstate ] || return 0
  remote_backend
  local i
  for i in $(seq 1 20); do
    if terraform init -input=false -no-color -migrate-state -force-copy "${backend[@]}" >/dev/null 2>&1; then
      echo "State moved into $state_account."
      return 0
    fi
    echo "Waiting for data access to $state_account ($i/20)"
    sleep 30
  done
  # Never lose it: keep the state with the run so a rerun can move it.
  mkdir -p "$AGENT_TEMPDIRECTORY/state-fallback" && cp terraform.tfstate "$AGENT_TEMPDIRECTORY/state-fallback/"
  echo "##vso[artifact.upload containerfolder=state-fallback;artifactname=state-fallback]$AGENT_TEMPDIRECTORY/state-fallback/terraform.tfstate"
  echo "Could not move the state into $state_account; it is kept as the run's state-fallback artifact." >&2
  exit 1
}
