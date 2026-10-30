#!/usr/bin/env bash
# workspace-apply.sh <gcp|azure> [workspace-dir...]
#
# Without workspace arguments, takes the terraform/workspaces/<cloud>_* directories
# changed by the last commit. Each one: init (backend.tf.json), plan, apply, then
# `python -m console.deliver wire` with the outputs. GCP workspaces log in to
# GCP through WIF as the setup service account; Azure ones use the AzureCLI@2
# login. The JFrog admin token comes from the setup connection's OIDC token.
set -euo pipefail
cd "$BUILD_SOURCESDIRECTORY"

cloud=${1:?usage: workspace-apply.sh <gcp|azure> [workspace-dir...]}
shift
case "$cloud" in
  gcp) export GCP_SERVICE_ACCOUNT="${GCP_SETUP_SERVICE_ACCOUNT:?}" ;;
  azure) ;;
  *) echo "unknown cloud: $cloud" >&2; exit 2 ;;
esac

if [ "$#" -gt 0 ]; then
  workspaces=("$@")
else
  mapfile -t workspaces < <(git diff --name-only HEAD~1 HEAD -- "terraform/workspaces/${cloud}_*" \
    | cut -d/ -f1-3 | sort -u)
fi
[ "${#workspaces[@]}" -gt 0 ] || { echo "no $cloud workspace changed"; exit 0; }
for ws in "${workspaces[@]}"; do
  case "$(basename "$ws")" in "${cloud}"_*) ;; *) echo "$ws is not a $cloud workspace" >&2; exit 2 ;; esac
done

# shellcheck source=pipelines/scripts/oidc-login.sh
source pipelines/scripts/oidc-login.sh
JFROG_ACCESS_TOKEN=$(python3 -m jfrog_oidc token)
export JFROG_ACCESS_TOKEN
export PYTHONPATH="$BUILD_SOURCESDIRECTORY/client/src:$BUILD_SOURCESDIRECTORY/rotator:$BUILD_SOURCESDIRECTORY"

for ws in "${workspaces[@]}"; do
  [ -f "$ws/main.tf.json" ] || { echo "$ws: removed, skipping"; continue; }
  echo "== $ws"
  terraform -chdir="$ws" init -input=false -no-color
  terraform -chdir="$ws" plan -input=false -no-color -lock-timeout=5m -out=tfplan
  terraform -chdir="$ws" apply -input=false -no-color -lock-timeout=5m tfplan
  terraform -chdir="$ws" output -json > "$OIDC_DIR/outputs.json"
  python3 -m console.deliver wire "$ws" "$OIDC_DIR/outputs.json"
done
