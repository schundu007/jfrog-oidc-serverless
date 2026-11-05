#!/usr/bin/env bash
# One-time, on a fresh Azure DevOps project: register the Azure pipelines from this repo.
# The consoles find each pipeline by its YAML path, so the names and folders are only for people.
# Skips any YAML already registered. The AKS credential provider's own pipeline
# (pipelines/oidc/azure/aks-jfrog-trust) is registered by terraform/stacks/oidc/azure/azure-jfrog-trust.
#
#   pipelines/scripts/register-azure-pipelines.sh <ado-org> <ado-project> <ado-repo> [branch]
#
# Needs: az login with the azure-devops extension (az extension add --name azure-devops).
set -euo pipefail
org=${1:?ado org}; project=${2:?ado project}; repo=${3:?ado repo}; branch=${4:-main}
cd "$(dirname "$0")/../.."
base=(--organization "https://dev.azure.com/$org" --project "$project")

list=("pipelines/azure-workload-onboard.yml|Azure - Onboard workloads to JFrog|\\Azure"
      "pipelines/kubernetes/azure/aks-cluster/stack-setup.yml|AKS cluster - Infrastructure|\\Kubernetes\\Azure")
for pair in "azure-web-app:Web App" "azure-container-app:Container App" "azure-container-apps:Container Apps job" \
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
