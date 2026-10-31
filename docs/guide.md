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

**not run yet**

## 1. Start the console

**not run yet**

## 2. Google Cloud foundation

**not run yet**

## 3. Azure foundation

**not run yet**

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
