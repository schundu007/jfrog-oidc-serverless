# Run it in another environment (fresh start)

From an empty Azure subscription, Azure DevOps project and JFrog instance to a workload pulling from JFrog with short-lived OIDC tokens. Nothing long-lived is stored anywhere. No value in this repo names your account: every one is entered in the UI.

## You need

| What | Detail |
|---|---|
| Azure subscription | Owner on it, and allowed to create app registrations in its Entra tenant |
| Azure DevOps | An organization and an empty project; you are a Project Administrator |
| JFrog Platform | SaaS or self-hosted, with an admin access token (kept in memory only, never saved) |
| Laptop | Python 3.9+, Terraform 1.17+, git, Azure CLI with `az extension add --name azure-devops` |
| Agents | Microsoft-hosted `ubuntu-24.04` (a new ADO organization must [request the free grant](https://aka.ms/azpipelines-parallelism-request)), or your own pool (below) |

## Everything from ADO (no laptop)

Every step runs as an ADO pipeline. The console (UI) is optional: it runs the same pipelines.

**By hand, once:**

| Step | Where |
|---|---|
| Put this code in an ADO repo | Repos → Import repository (from github.com/schundu007/jfrog-oidc-serverless, or upload the zip) |
| Bootstrap service connection `jfrog-oidc-azure-bootstrap` | Project settings → Service connections → Azure Resource Manager → Workload identity federation (automatic); tick "Grant access permission to all pipelines" |
| Rights for its identity | Owner on the subscription; in Entra, Application Administrator and Privileged Role Administrator (it creates the audience app and grants app roles) |
| Its identity in ADO | Organization settings → Users → add it (Basic); Project settings → Permissions → Project Administrators |
| The first pipeline | Pipelines → New pipeline → Azure Repos Git → this repo → Existing YAML → `/pipelines/azure-register-pipelines.yml` |

**Then run, in order:**

| # | Pipeline | What it does |
|---|---|---|
| 1 | Azure - Register pipelines | Registers every other Azure pipeline |
| 2 | Pipeline identity and state - Infrastructure | State storage, build/setup identities, service connections `jfrog-oidc-azure-build`/`-setup`, variable group `jfrog-oidc-azure-<env>`, approval environments. Edit the `inputs` parameter (location, operators' emails); tenant, subscription and ADO org/project come from the run |
| 3 | Pipelines' JFrog trust - JFrog OIDC | First run only: add the secret pipeline variable `JFROG_ADMIN_TOKEN` and set `jfrog_url` in `inputs`. Creates the JFrog integration, mappings, repositories and variable group `jfrog-oidc-azure-<env>-jfrog`. Then delete the variable: later runs sign in to JFrog with OIDC |
| 4 | `<stack>` - Infrastructure | One per workload type (Web App, Container App, …); fill `inputs` (name, subscription, location) |
| 5 | `<stack>` - Build and deploy | Builds through JFrog with OIDC and rolls out |
| 6 | Kubernetes (optional) | `aks-cluster` - Infrastructure, then `aks-jfrog-trust` (registered by step 3) |

Each Infrastructure run stops for approval on its ADO environment; approve it there. The first run of a pipeline may ask to **Permit** a connection or environment once.

Onboarding a workload's own identity to JFrog (`Azure - Onboard workloads to JFrog`) applies the workspaces that the console proposes by pull request, so that step needs the UI once.

## Steps (with the UI)

1. **Package.** In the environment you copy from, build the portable zip. It leaves out that environment's own data (its onboarding workspaces `terraform/workspaces/`, the `ado-pipelines/` mirror); nothing in it names an account, subscription, tenant, ADO organization or JFrog instance (`portal/tests/test_portable.py` checks this).
   ```bash
   pipelines/scripts/export.sh            # jfrog-oidc-serverless-<commit>.zip
   ```
   Unzip it in the new environment and `git init && git add -A && git commit -m import`.
2. **Repo and pipelines in ADO** (one time). Creates the repo when missing, pushes this code to `main`, and registers the Azure pipelines:
   ```bash
   az login
   pipelines/scripts/register-azure-pipelines.sh <ado-org> <ado-project> <ado-repo>
   ```
3. **Start the console** and open the URL it prints:
   ```bash
   PYTHONPATH=client/src:. python3 -m portal
   ```
4. **Settings.** Sign in to Azure. Enter the ADO organization, project and repo. Enter the JFrog URL and admin token.
5. **Infrastructure → Pipeline identity and state** (`azure-pipeline-identity`). Fill in the tenant, subscription, location, ADO org and project, and the operators' emails. Then Deploy and approve the plan. It creates:
   - the state storage
   - the build and setup identities
   - the ADO service connections `jfrog-oidc-azure-build` and `jfrog-oidc-azure-setup` (workload identity federation, no secrets)
   - the variable group `jfrog-oidc-azure-<env>`
   - the ADO environments for the approvals
6. **JFrog OIDC → Azure** (`azure-jfrog-trust`). Deploy and approve. It creates:
   - the JFrog OIDC integration and the `ado-build`/`ado-setup` mappings
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

## What changes per environment

All of it is entered in the UI; nothing is edited in `terraform/` and nothing in the repo names your account.

| Value | Where |
|---|---|
| ADO organization, project, repo, branch | Settings → Azure DevOps (`pipelines/delivery.json` ships empty: the console starts in local mode and switches to ADO as soon as you save) |
| Tenant, subscription, location, operators | Infrastructure → the stack's inputs form |
| JFrog URL | Settings, then JFrog OIDC → the trust stack's inputs |
| Region | Every stack's `location` input; nothing is tied to one region |
| Terraform state | Created by the pipeline identity stack: storage account `st<org, 12 chars><subscription, 8 chars>`, unique per organization and subscription, so a second subscription under the same organization never collides |
| JFrog repository names | `pipelines/vars/azure-<env>-jfrog.yml`; `azure-jfrog-trust` creates them in your JFrog |

**Self-hosted agents.** Set `AGENT_POOL` in `pipelines/vars/azure-<env>.yml` (and `gcp-<env>.yml`) to your pool's name. Empty means Microsoft-hosted `ubuntu-24.04`. The agents need bash, Python 3, curl, unzip, git and the Azure CLI; build-deploy also needs Docker.

**Another environment name.** `test` is the default. For `prod`, copy `pipelines/vars/azure-test.yml` and `azure-test-jfrog.yml` to `azure-prod*.yml`, add `prod` to the `environment` parameter's `values` in the pipelines you use, and set `"environment": "prod"` in `pipelines/delivery.json`. The pipeline identity stack then writes the variable groups `jfrog-oidc-azure-prod` and `jfrog-oidc-azure-prod-jfrog`.

The first run of each pipeline may stop on "This pipeline needs permission to access a resource". Choose **Permit** once for the service connection and the environment.

## Which identity does what

| Identity | Used by | For |
|---|---|---|
| ADO service connection `-setup` (workload identity federation) | Infrastructure pipelines | Terraform plan and apply |
| ADO service connection `-build` (workload identity federation) | Build and deploy, onboarding | Push and deploy the app, resolve packages through JFrog |
| User-assigned managed identity, one per deployment | The running app | Entra token → JFrog access token, at runtime |
| AKS OIDC issuer (Entra Workload ID) | Kubelet credential provider | Image pulls from JFrog on AKS nodes |
