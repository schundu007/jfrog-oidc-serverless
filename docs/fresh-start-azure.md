# Fresh start on a new Azure account

From an empty Azure subscription, Azure DevOps project and JFrog instance to a workload pulling from JFrog with short-lived OIDC tokens. Nothing long-lived is stored anywhere. No value in this repo names your account: every one is entered in the UI.

## You need

| What | Detail |
|---|---|
| Azure subscription | Owner on it, and allowed to create app registrations in its Entra tenant |
| Azure DevOps | An organization and an empty project; you are a Project Administrator |
| JFrog Platform | SaaS or self-hosted, with an admin access token (kept in memory only, never saved) |
| Laptop | Python 3.9+, Terraform 1.17+, git, Azure CLI with `az extension add --name azure-devops` |

## Steps

1. **Repo in ADO.** Create a repo in the project and push this code to its `main`. The pipelines run from it.
2. **Register the pipelines** (one time):
   ```bash
   az login
   pipelines/scripts/register-azure-pipelines.sh <ado-org> <ado-project> <ado-repo>
   ```
3. **Start the console** and open the URL it prints:
   ```bash
   PYTHONPATH=client/src:. python3 -m portal
   ```
4. **Settings.** Sign in to Azure. Enter the ADO organization, project and repo. Enter the JFrog URL and admin token.
5. **Infrastructure → DevOps integrations** (`azure-foundation`). Fill in the tenant, subscription, location, ADO org and project, and the operators' emails. Then Deploy and approve the plan. It creates:
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

The first run of each pipeline may stop on "This pipeline needs permission to access a resource". Choose **Permit** once for the service connection and the environment.

## Which identity does what

| Identity | Used by | For |
|---|---|---|
| ADO service connection `-setup` (workload identity federation) | Infrastructure pipelines | Terraform plan and apply |
| ADO service connection `-build` (workload identity federation) | Build and deploy, onboarding | Push and deploy the app, resolve packages through JFrog |
| User-assigned managed identity, one per deployment | The running app | Entra token → JFrog access token, at runtime |
| AKS OIDC issuer (Entra Workload ID) | Kubelet credential provider | Image pulls from JFrog on AKS nodes |
