# ADO pipelines

Snapshot of the pipelines registered in Azure DevOps, per project. The live copy runs from the ADO repo; refresh this directory after changing it there.

## cdoazure/jfrog-cp-azure (Azure, from jfrog-cp-azure f0dfded)

`jfrog-cp-azure/pipelines/`: the YAML and scripts. `jfrog-cp-azure/definitions.json`: the registered pipelines.

| Id | Folder | Pipeline | YAML |
|---|---|---|---|
| 35 | \Azure | Azure - Onboard workloads to JFrog | `pipelines/azure-workload-onboard.yml` |
| 49 | \JFrog OIDC\Azure | AKS credential provider - JFrog OIDC | `pipelines/oidc/azure/aks-jfrog-trust/stack-setup.yml` |
| 48 | \Kubernetes\Azure | AKS cluster - Infrastructure | `pipelines/kubernetes/azure/aks-cluster/stack-setup.yml` |
| 39 | \Serverless\Azure\Container App | Container App - Build and deploy | `pipelines/serverless/azure/azure-container-app/build-deploy.yml` |
| 38 | \Serverless\Azure\Container App | Container App - Infrastructure | `pipelines/serverless/azure/azure-container-app/stack-setup.yml` |
| 41 | \Serverless\Azure\Container Apps job | Container Apps job - Build and deploy | `pipelines/serverless/azure/azure-container-apps/build-deploy.yml` |
| 40 | \Serverless\Azure\Container Apps job | Container Apps job - Infrastructure | `pipelines/serverless/azure/azure-container-apps/stack-setup.yml` |
| 45 | \Serverless\Azure\Functions Flex Consumption | Functions Flex Consumption - Build and deploy | `pipelines/serverless/azure/azure-functions-flex/build-deploy.yml` |
| 44 | \Serverless\Azure\Functions Flex Consumption | Functions Flex Consumption - Infrastructure | `pipelines/serverless/azure/azure-functions-flex/stack-setup.yml` |
| 47 | \Serverless\Azure\Functions Premium | Functions Premium - Build and deploy | `pipelines/serverless/azure/azure-functions-premium/build-deploy.yml` |
| 46 | \Serverless\Azure\Functions Premium | Functions Premium - Infrastructure | `pipelines/serverless/azure/azure-functions-premium/stack-setup.yml` |
| 43 | \Serverless\Azure\Functions on Container Apps | Functions on Container Apps - Build and deploy | `pipelines/serverless/azure/azure-functions-aca/build-deploy.yml` |
| 42 | \Serverless\Azure\Functions on Container Apps | Functions on Container Apps - Infrastructure | `pipelines/serverless/azure/azure-functions-aca/stack-setup.yml` |
| 37 | \Serverless\Azure\Web App | Web App - Build and deploy | `pipelines/serverless/azure/azure-web-app/build-deploy.yml` |
| 36 | \Serverless\Azure\Web App | Web App - Infrastructure | `pipelines/serverless/azure/azure-web-app/stack-setup.yml` |

GCP, Kubernetes and OpenShift pipelines run from `pipelines/` at the repo root (project cloudifyDevops).
