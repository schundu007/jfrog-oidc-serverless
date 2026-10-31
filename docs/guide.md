# Provisioning guide: infrastructure, each serverless service, and JFrog OIDC

Every step below was run end to end on a fresh GCP project and Azure subscription, in this
order, and the result shown is what that run produced. A section marked **not run yet** has no
steps until it has been done for real.

What you end up with: workloads on Google Cloud and Azure that read from JFrog with a JFrog
token they get by OIDC exchange of their own cloud identity. No JFrog token, password or key is
stored anywhere: not in the cloud, not in Azure DevOps, not on your machine.

## Contents

0. Before you start (accounts, tools, permissions)
1. Start the console
2. Google Cloud foundation
3. Azure foundation
4. Google Cloud workloads, one at a time
   - 4.1 Cloud Run job
   - 4.2 Cloud Run service
   - 4.3 Cloud Run function
5. Azure workloads, one at a time
   - 5.1 Container Apps job
   - 5.2 Container App
   - 5.3 Functions on Container Apps
   - 5.4 Functions Premium / Dedicated
   - 5.5 Functions Flex Consumption
6. JFrog OIDC for a workload (the Serverless console)
7. Kubernetes clusters (GKE, AKS) and the Kubernetes console
8. Tear down
9. Problems seen on the way and their fixes

## 0. Before you start

Accounts (the run used test accounts; any you control work):

| Account | Used in the run | You need |
|---|---|---|
| Google Cloud project | `unioxe` | Owner on the project (your `gcloud` login) |
| Azure subscription | `cra-ac-sub` | Owner on the subscription (your `az` login): the foundation assigns roles |
| Azure DevOps | org `cdoazure`, project `cloudifyDevops` | Project Administrator (creates service connections, variable groups, environments) |
| JFrog platform | `https://trial9hnw59.jfrog.io` | An admin, once: to make a bootstrap admin token |

JFrog must already have: a Docker remote repository (`docker-remote`, Docker Hub), a PyPI remote
(`pypi-remote`) and a group workloads and pipelines read with (`serverless-readers`). Nothing else.

Tools on your machine (versions used in the run):

| Tool | Version | Check |
|---|---|---|
| Terraform | 1.17.1 | `terraform version` |
| Google Cloud CLI | 555.0.0 | `gcloud version` |
| Azure CLI | 2.86.0 | `az version` |
| Python | 3.12 or newer (run: 3.14.7), no packages | `python3 --version` |
| git | any | |

Sign in once:

```bash
gcloud auth login                      # your Google account (Owner on the project)
gcloud auth application-default login  # the same account, for Terraform
az login                               # your Azure account (Owner on the subscription)
```

Bootstrap JFrog admin token (only for the first deploy of the Google Cloud foundation, which
creates the console's own OIDC login to JFrog; after that no stored token is used):
JFrog → Administration → User Management → Access Tokens → **Generate Token** → Scoped Token,
Token scope **Admin**, Expiration **Custom, 8 hours** → Generate → Copy. Save it to a file only
you can read, e.g. `~/.jfrog-test-admin-token` (`chmod 600`). Never paste it into a form, a
chat or a commit.

## 1. Start the console

From the repository root:

```bash
JFROG_ACCESS_TOKEN="$(cat ~/.jfrog-test-admin-token)" \
  PYTHONPATH=client/src:rotator:. python3 -m portal --no-browser
```

It prints `JFrog OIDC console (Serverless /serverless, Kubernetes /k8s, Setup /setup): http://127.0.0.1:8760/#t=<session>`.
Open that link; the console only listens on 127.0.0.1 and needs the `#t=` session in the link.
Go to **Setup → Infrastructure**. The page has two sections, **Serverless** and **Kubernetes**;
inside each, one card per cloud and one row per stack. Every row deploys and destroys on its own.

How every Deploy works (same for every row):

1. **Inputs** → fill the form → **Save**. The row shows *Inputs needed* until every required input is saved.
2. **Deploy** → the console runs `terraform plan` and shows each resource it would add, change or delete.
3. **Approve and deploy** → it applies exactly that plan (bound to the plan's sha256). Nothing changes before this click.
4. The row turns *Deployed* and shows the stack's outputs and what to do next.

Destroy is the same, and also asks you to type the stack name.

## 2. Google Cloud foundation

Row: Serverless → Google Cloud → **Foundation (ADO trust, state)**. Deploy it first: every
Google Cloud workload stack keeps its Terraform state in the bucket it creates, and the
pipelines sign in with the identities it creates.

Inputs used in the run:

| Input | Value | Meaning |
|---|---|---|
| `jfrog_url` | `https://trial9hnw59.jfrog.io` | JFrog origin, https, no path |
| `jfrog_reader_group` | `serverless-readers` | JFrog group the pipeline tokens get (read) |
| `gcp_project_id` | `unioxe` | Project everything goes into |
| `gcp_region` | `us-central1` | Region (default) |
| `gcp_image_repository` | `platform` | Artifact Registry repository for images (default) |
| `ado_org` / `ado_project` | `cdoazure` / `cloudifyDevops` | Azure DevOps organization and project |
| `ado_tenant_id` | the Entra tenant of the ADO org | Issuer of the pipelines' OIDC tokens |
| `ado_token_audience` | the `aud` the run's ADO tokens carry | Default `api://AzureADTokenExchange`; check with `python -m jfrog_oidc claims` in a pipeline |
| `pipeline_environment` | `test` | Names the variable group `jfrog-oidc-gcp-test` |
| `console_operators` | `["user:<your Google account>"]` | Who may act as the console operator |
| `ado_approvers` | `["<your ADO sign-in>"]` | Who approves each stack's pipeline apply |

Result of the run: **47 added, 1 changed** (the existing ADO environment
`gcp-cloud-run-test` was imported). Outputs:

| Output | Value in the run |
|---|---|
| Pipeline service accounts | `ado-build@unioxe.iam.gserviceaccount.com`, `ado-setup@unioxe.iam.gserviceaccount.com` |
| Workload identity provider | `projects/851540983072/locations/global/workloadIdentityPools/azure-devops-d968/providers/cdoazure` |
| JFrog OIDC integration for the pipelines | `ado-cdoazure-gcp` |
| Console operator | `jfrog-console-operator@unioxe.iam.gserviceaccount.com` via JFrog integration `console-operator-google` |
| Terraform state bucket | `unioxe-tfstate` |

It also creates, in Azure DevOps: service connections `jfrog-oidc-gcp-build` and
`jfrog-oidc-gcp-setup`, variable group `jfrog-oidc-gcp-test`, and one environment with an
approval check per Google Cloud workload stack (`gcp-cloud-run-test`,
`gcp-cloud-run-service-test`, `gcp-cloud-run-function-test`).

From here on the console gets its JFrog token by exchanging the operator's Google ID token;
start it without `JFROG_ACCESS_TOKEN` and revoke the bootstrap token.

## 3. Azure foundation

Row: Serverless → Azure → **Foundation (ADO trust, state)**. Same role as the Google Cloud one,
for Azure. It shares nothing with Google Cloud.

Inputs used in the run:

| Input | Value | Meaning |
|---|---|---|
| `jfrog_url`, `jfrog_reader_group`, `ado_org`, `ado_project`, `ado_token_audience`, `pipeline_environment`, `ado_approvers` | as in §2 | |
| `azure_tenant_id` | the Entra tenant | Issuer of the service connections' tokens |
| `azure_subscription_id` | `0e56d318-75b3-43ef-a41b-6a1a3c95b458` | Subscription everything goes into |
| `azure_location` | `westus2` | Region (default) |
| `azure_identity` | `{"name": "id-ado-cdoazure", "resource_group": "ado-cicd-rg"}` | Names of the build identity and the resource group; the stack creates both |
| `azure_console_operators` | your Entra object ID | May read and write the Azure Terraform state from the console |

Result of the run: **31 added, 1 changed** (environment `azure-container-apps-test` imported). Outputs:

| Output | Value in the run |
|---|---|
| Resource group | `ado-cicd-rg` (build identity `id-ado-cdoazure`, setup identity `id-ado-cdoazure-setup`) |
| Setup identity client ID | `81b613bd-daba-4df0-b618-d79f0b75d04d` |
| Terraform state | storage account `stcdoazuretfstate`, container `tfstate` |
| JFrog OIDC integration for the pipelines | `ado-cdoazure-azure` |

In Azure DevOps: service connections `jfrog-oidc-azure-build` / `jfrog-oidc-azure-setup`,
variable group `jfrog-oidc-azure-test`, and an environment with an approval check per Azure
workload stack (`azure-container-apps-test`, `azure-container-app-test`,
`azure-functions-aca-test`, `azure-functions-premium-test`, `azure-functions-flex-test`).

## 4. Google Cloud workloads

**not run yet**

## 5. Azure workloads

**not run yet**

## 6. JFrog OIDC for a workload

**not run yet**

## 7. Kubernetes clusters

**not run yet**

## 8. Tear down

Run on 2026-10-31 against GCP project `unioxe` and Azure subscription `cra-ac-sub`; every step
finished with the result shown. Order matters: each step removes something a later one would
otherwise still need.

| # | What | How | Result |
|---|---|---|---|
| 1 | Each cloud's console workspace (`terraform/workspaces/<cloud>_*`): the workloads' JFrog integration and mappings, the Entra audience app | `terraform destroy` in the workspace, with a JFrog admin token in `JFROG_ACCESS_TOKEN` | GCP 2 destroyed, Azure 7 destroyed |
| 2 | Each workload stack that is deployed | Infrastructure page → the stack's row → **Destroy** → type the stack name → **Approve and destroy** | Cloud Run job 3 destroyed; Container Apps job 9 destroyed (about 10 minutes: the Container Apps environment is slow to delete) |
| 3 | Anything created outside Terraform | Delete by hand. In this run: Cloud Run service `jfrog-svc-demo`, function `jfrog-fn-demo`, the Cloud Functions build repository `gcf-artifacts` and its buckets `gcf-v2-sources-*`, `gcf-v2-uploads-*` | Deleted |
| 4 | Azure foundation | Infrastructure page → Serverless → Azure → Foundation → **Destroy** | 20 destroyed |
| 5 | GCP foundation (last: it holds the console operator identity that gives the JFrog admin token for steps 1, 2 and 4) | Empty the state bucket first (`gcloud storage rm -r --all-versions "gs://<project>-tfstate/**"`), then **Destroy** | 39 destroyed |

What is left afterwards: in GCP only the project's default service accounts
(`<number>-compute@developer...`, `<project>@appspot...`); in Azure no resource group; in Azure
DevOps no service connection and no variable group. The saved inputs
(`.console/setup/<env>/*.tfvars.json`) stay, so a rebuild starts with the forms filled in.

## 9. Problems seen on the way

| Seen | Cause | Fix (in the code now) |
|---|---|---|
| GCP foundation destroy would fail on the state bucket | The bucket still holds the destroyed stacks' (empty) state files | Teardown step 5 empties it first |
| A new GCP foundation cannot reuse the pool id `azure-devops` after a destroy | GCP keeps a deleted workload identity pool for 30 days and refuses its id | The pool id now gets a random suffix (`azure-devops-<4 hex>`) on each deploy |
| The Azure foundation left `ado-cicd-rg` and `id-ado-cdoazure` behind | They were created by hand before the first deploy and only read by the foundation | The foundation now creates the resource group and the build identity itself (names from the `azure_identity` input) |
