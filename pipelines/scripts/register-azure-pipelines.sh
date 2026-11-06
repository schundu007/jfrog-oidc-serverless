#!/usr/bin/env bash
# On a fresh Azure DevOps project: register the Azure pipelines from this repo. Runs in ADO as the pipeline
# "Azure - Register pipelines" (pipelines/azure-register-pipelines.yml, through the bootstrap service
# connection), or from a machine with az login, where it also creates the repo and pushes this code when missing.
# The consoles find each pipeline by its YAML path, so the names and folders are only for people.
# Skips any YAML already registered.
#
#   pipelines/scripts/register-azure-pipelines.sh <ado-org> <ado-project> <ado-repo> [branch]
#
# Needs: az login (or the AzureCLI@2 login in ADO); the azure-devops extension is added when missing.
set -euo pipefail
org=${1:?ado org}; project=${2:?ado project}; repo=${3:?ado repo}; branch=${4:-main}
cd "$(dirname "$0")/../.."
base=(--organization "https://dev.azure.com/$org" --project "$project")

az extension show --name azure-devops -o none 2>/dev/null || az extension add --name azure-devops --only-show-errors

# The repo: created when missing; this code pushed to its branch when that branch does not exist yet.
# In an ADO run (TF_BUILD) the code already is in the repo the run checked out.
if [ -z "${TF_BUILD:-}" ]; then
  if ! az repos show "${base[@]}" --repository "$repo" -o none 2>/dev/null; then
    az repos create "${base[@]}" --name "$repo" -o none
    echo "created repo $repo"
  fi
  url="https://dev.azure.com/$org/$project/_git/$repo"
  if [ -z "$(git ls-remote --heads "$url" "$branch" 2>/dev/null)" ]; then
    git push "$url" "HEAD:refs/heads/$branch"
    echo "pushed $(git rev-parse --short HEAD) to $repo/$branch"
  fi
fi

list=("pipelines/azure-register-pipelines.yml|Azure - Register pipelines|\\Azure"
      "pipelines/serverless/azure/azure-pipeline-identity/stack-setup.yml|Pipeline identity and state - Infrastructure|\\Serverless\\Azure"
      "pipelines/oidc/azure/azure-jfrog-trust/stack-setup.yml|Pipelines' JFrog trust - JFrog OIDC|\\JFrog OIDC\\Azure"
      "pipelines/azure-workload-onboard.yml|Azure - Onboard workloads to JFrog|\\Azure"
      "pipelines/app-services/azure/azure-web-app/stack-setup.yml|Web App - Infrastructure|\\App Services\\Azure\\Web App"
      "pipelines/app-services/azure/azure-web-app/build-deploy.yml|Web App - Build and deploy|\\App Services\\Azure\\Web App")
for pair in "azure-container-app:Container App" "azure-container-apps:Container Apps job" \
            "azure-functions-aca:Functions on Container Apps" "azure-functions-premium:Functions Premium" \
            "azure-functions-flex:Functions Flex Consumption"; do
  stack=${pair%%:*}; t=${pair#*:}
  list+=("pipelines/serverless/azure/$stack/stack-setup.yml|$t - Infrastructure|\\Serverless\\Azure\\$t"
         "pipelines/serverless/azure/$stack/build-deploy.yml|$t - Build and deploy|\\Serverless\\Azure\\$t")
done

have=$(az pipelines list "${base[@]}" --query "[].process.yamlFilename" -o tsv 2>/dev/null || true)
for row in "${list[@]}"; do
  IFS='|' read -r yaml name folder <<<"$row"
  [ -f "$yaml" ] || { echo "skip $yaml: not in this repo"; continue; }
  if grep -qxF -e "$yaml" -e "/$yaml" <<<"$have"; then echo "exists  $name"; continue; fi
  az pipelines create "${base[@]}" --name "$name" --folder-path "$folder" --repository "$repo" \
    --repository-type tfsgit --branch "$branch" --yaml-path "$yaml" --skip-first-run true -o none
  echo "created $name"
done
