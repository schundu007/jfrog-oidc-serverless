# Azure deployment guide

Deploys the JFrog OIDC integration into an Azure subscription, an Azure DevOps project and a JFrog Platform instance, up to workloads that use short-lived OIDC tokens. No long-lived credential is stored. Environment-specific values are supplied as pipeline run parameters or in the web console.

## Prerequisites

| What | Detail |
|---|---|
| Azure subscription | Owner on it, and allowed to create app registrations in its Entra tenant |
| Azure DevOps | An organization and an empty project; you are a Project Administrator |
| JFrog Platform | SaaS or self-hosted; an administrator account for the one-time OIDC integration |
| Workstation (web console only) | Python 3.9+, Terraform 1.17+, git, Azure CLI with the `azure-devops` extension |
| Agents | Microsoft-hosted `ubuntu-24.04` (a new ADO organization must [request the free grant](https://aka.ms/azpipelines-parallelism-request)), or your own pool (below) |

## Deployment with Azure DevOps pipelines

Every step runs as an Azure DevOps pipeline. The web console is optional and queues the same pipelines.

### One-time setup

| Step | Where |
|---|---|
| Source repository | Repos → Import repository (this repository or your fork) |
| Bootstrap service connection `jfrog-oidc-azure-bootstrap` | Project settings → Service connections → Azure Resource Manager → Workload identity federation (automatic); tick "Grant access permission to all pipelines" |
| Bootstrap identity permissions | Owner on the subscription; in Entra, Application Administrator and Privileged Role Administrator (it creates the audience app and grants app roles) |
| Bootstrap identity in Azure DevOps | Organization settings → Users → add it (Basic); Project settings → Permissions → Project Administrators |
| Register pipelines pipeline | Pipelines → New pipeline → Azure Repos Git → this repo → Existing YAML → `/pipelines/azure-register-pipelines.yml` |

### JFrog OIDC integration (one time, between steps 2 and 3)

Created by a JFrog administrator. No token is created or stored; step 3 imports both objects into Terraform.

| Object | Values |
|---|---|
| Administration → General → Manage Integrations → New → OpenID Connect | Name `ado-<ado-org>-azure`, type Generic, provider URL `https://login.microsoftonline.com/<tenant-id>/v2.0`, audience `api://AzureADTokenExchange` |
| Its identity mapping `ado-setup` | Priority 2; claims `{"sub": "<subject identifier>", "aud": "api://AzureADTokenExchange"}`, the subject copied from ADO → Project settings → Service connections → `jfrog-oidc-azure-setup` (exists after step 2); token scope Admin, username `ado-setup`, expiry 3600 |

### Deployment sequence

| # | Pipeline | What it does |
|---|---|---|
| 1 | Azure - Register pipelines | Registers every other Azure pipeline |
| 2 | Pipeline identity and state - Infrastructure | State storage, build/setup identities, service connections `jfrog-oidc-azure-build`/`-setup`, variable group `jfrog-oidc-azure-<env>`, approval environments. Edit the `inputs` parameter (location, operators' emails); tenant, subscription and ADO org/project come from the run |
| 3 | Pipelines' JFrog trust - JFrog OIDC | Set `jfrog_url` and `ado_org` in `inputs`. Signs in to JFrog with OIDC (no JFrog token anywhere), imports the two objects created in the JFrog UI, creates `ado-build`, the reader group, repositories and variable group `jfrog-oidc-azure-<env>-jfrog` |
| 4 | `<stack>` - Infrastructure | One per workload type: Web App, Container App, Container Apps job, Functions on Container Apps, Functions Premium, Functions Flex Consumption. Fill `inputs` (name, subscription, location) |
| 5 | `<stack>` - Build and deploy | Builds through JFrog with OIDC and rolls out; set the `workload` parameter |
| 6 | Azure - Onboard workloads to JFrog | Applies the workload's JFrog mapping (proposed by pull request from the web console) and sets its `JFROG_*` app settings |
| 7 | Kubernetes (optional) | `AKS cluster - Infrastructure`, then the `aks-jfrog-trust` pipeline (registered by step 3) |

Each Infrastructure run stops for approval on its ADO environment; approve it there. The first run of a pipeline may ask to **Permit** a connection or environment once.

## Deployment with the web console

1. **Source package (optional).** `pipelines/scripts/export.sh` builds a zip without environment data (onboarding workspaces, the `ado-pipelines/` mirror); `portal/tests/test_portable.py` checks that it names no account, subscription, tenant, organization or JFrog instance.
2. **Repository and pipelines.** Creates the repository when missing, pushes the code to `main`, and registers the Azure pipelines:
   ```bash
   az login
   pipelines/scripts/register-azure-pipelines.sh <ado-org> <ado-project> <ado-repo>
   ```
3. **Start the web console** and open the URL it prints:
   ```bash
   PYTHONPATH=client/src:. python3 -m portal
   ```
4. **Settings.** Sign in to Azure. Enter the Azure DevOps organization, project and repository, and the JFrog URL.
5. **Infrastructure → Pipeline identity and state** (`azure-pipeline-identity`). Fill in the tenant, subscription, location, ADO org and project, and the operators' emails. Then Deploy and approve the plan. It creates:
   - the state storage
   - the build and setup identities
   - the ADO service connections `jfrog-oidc-azure-build` and `jfrog-oidc-azure-setup` (workload identity federation, no secrets)
   - the variable group `jfrog-oidc-azure-<env>`
   - the ADO environments for the approvals
6. **JFrog OIDC → Azure** (`azure-jfrog-trust`). Deploy and approve. It creates:
   - imports the JFrog OIDC integration and `ado-setup` mapping, and creates the `ado-build` mapping
   - the remote repositories
   - the variable group `jfrog-oidc-azure-<env>-jfrog`
   - the AKS credential provider pipeline
7. **Serverless → + Create**, then pick the type: Web App, Container App, Container Apps job, Functions (Flex, Premium, on Container Apps). Give a name, subscription and location. Every deployment gets its own new resource group `rg-<name>`, identity and Terraform state, and never touches another one. One Deploy runs the whole chain:
   1. infrastructure plan
   2. your approval
   3. apply
   4. build and deploy
   5. onboarding to JFrog
   6. the post-deployment check
8. **Kubernetes (optional).** Run Infrastructure → Kubernetes → `aks-cluster`, then JFrog OIDC → `aks-jfrog-trust`. Nodes labelled `credentialsProviderEnabled` then pull images from JFrog with the cluster's OIDC issuer.

## Environment configuration

All values are supplied at run time; nothing under `terraform/` is edited.

| Value | Where |
|---|---|
| ADO organization, project, repo, branch | Pipeline run context, or Settings → Azure DevOps in the web console (`pipelines/delivery.json` is empty by default) |
| Tenant, subscription, location, operators | The stack's `inputs` run parameter, or Infrastructure → inputs form |
| JFrog URL | The trust stack's `inputs` run parameter, or JFrog OIDC → inputs form |
| Region | Each stack's `location` input |
| Terraform state | Created by the pipeline identity stack: storage account `st<org, 12 chars><subscription, 8 chars>`, unique per organization and subscription |
| JFrog repository names | `pipelines/vars/azure-<env>-jfrog.yml`; `azure-jfrog-trust` creates them in your JFrog |

### Self-hosted agents

Set `AGENT_POOL` in `pipelines/vars/azure-<env>.yml` (and `gcp-<env>.yml`) to your pool's name. Empty means Microsoft-hosted `ubuntu-24.04`. The agents need bash, Python 3, curl, unzip, git and the Azure CLI; build-deploy also needs Docker.

### Additional environments

`test` is the default. For `prod`, copy `pipelines/vars/azure-test.yml` and `azure-test-jfrog.yml` to `azure-prod*.yml`, add `prod` to the `environment` parameter's `values` in the pipelines you use, and set `"environment": "prod"` in `pipelines/delivery.json`. The pipeline identity stack then writes the variable groups `jfrog-oidc-azure-prod` and `jfrog-oidc-azure-prod-jfrog`.

## Identities

| Identity | Used by | For |
|---|---|---|
| ADO service connection `jfrog-oidc-azure-bootstrap` (workload identity federation) | Register pipelines, pipeline identity, pipelines' JFrog trust | Creates the pipeline identities and registers pipelines |
| ADO service connection `-setup` (workload identity federation) | Infrastructure pipelines, onboarding apply | Terraform plan and apply |
| ADO service connection `-build` (workload identity federation) | Build and deploy, onboarding | Push and deploy the app, resolve packages through JFrog |
| User-assigned managed identity `id-<name>`, one per deployment | The running app | Entra token → JFrog access token, at runtime |
| AKS OIDC issuer (Entra Workload ID) | Kubelet credential provider | Image pulls from JFrog on AKS nodes |
