# JFrog OIDC for serverless — project notes for Claude Code

Short-lived JFrog tokens for Cloud Run (services, jobs, functions), Azure Container Apps and Azure Functions (Flex, Premium/Dedicated, Functions on ACA), AWS Lambda, ECS tasks and App Runner, and Knative services (OpenShift Serverless or any Knative cluster), using OIDC token exchange. Nothing long-lived is stored anywhere.

We are now building **the console**: a local web UI that configures, simulates and verifies this for a target. The full spec is in `docs/console-spec.md`; read it before starting any milestone. Record progress in `docs/progress.md`.

## Layout

- `client/` — `jfrog_oidc`, a stdlib-only Python client: identity sources (GCP, Azure, AWS STS `GetWebIdentityToken` with its own SigV4 in `sigv4.py`, ADO, file), the exchange with caching, helpers and a CLI.
- `rotator/` — the pull-path rotator for GCP (Secret Manager + AR repoint) and Azure (Key Vault + ARM refresh).
- `terraform/modules/` — `jfrog-oidc`, `gcp-pull-path`, `azure-identity-and-pull-path`, `aws-identity` (dedicated roles and the `sts:GetWebIdentityToken` policy). `terraform/stacks/` holds the deployable root configs: `aks-cluster` and `gke-cluster` (test clusters, OIDC issuer on), `gcp-foundation` and `azure-foundation` (each cloud's own ADO trust: WIF or federated credentials, its own JFrog integration `ado-<org>-<cloud>` and mappings, its pipeline variable group `jfrog-oidc-<cloud>-<env>`, its state store; clouds share nothing; no AWS), `gcp-cloud-run`, `azure-container-apps` (one workload each) and `all-targets`. Stacks hold no target values: their inputs are filled in on the Infrastructure page.
- `apps/` — workload images and functions that use the client: `artifact-reader` (job for Cloud Run, Container Apps and ECS tasks), `cloud-run-service` (also the App Runner / ECS service image), `cloud-run-function`, `azure-function`, `aws-lambda`.
- `pipelines/` — Azure DevOps pipelines (the only CI/CD), separate per cloud: `<cloud>-stack-setup.yml` (infrastructure: parameters `stack` = `gcp-cloud-run`/`gke-cluster` or `azure-container-apps`/`aks-cluster`, `action` = apply/destroy; approval on the ADO environment `<stack>-<env>`), `<cloud>-build-deploy.yml` and `<cloud>-workload-onboard.yml` (applies and wires the workspaces the console proposes by pull request, `terraform/workspaces/<cloud>_*`). Every job authenticates with its service connection's OIDC token; ADO settings per cloud in `pipelines/vars/<cloud>-<env>.yml`; target values in the ADO variable group `jfrog-oidc-<cloud>-<env>`, written by that cloud's foundation stack. `pipelines/delivery.json` holds only the ADO repo; pipeline identities and state backends come from the foundation outputs.
- `console/` — the Serverless console, per the spec. Clouds: GCP, Azure, AWS (`steps/aws.py`, spec §11) and Knative (`steps/openshift.py`, spec §12; works on any cluster through a kubectl context). AWS and Knative are runtime path only.
- `setup_console/` — Setup at `/setup` (portal only): deploys and destroys **test infrastructure** where none exists yet (`gcp-foundation`, `gcp-cloud-run`, `gke-cluster`; `azure-foundation`, `azure-container-apps`, `aks-cluster`). Plan → approve the plan's sha256 → apply; destroy also needs the stack name typed. Inputs: a form generated from each stack's `variables.tf` (live project/subscription pickers), saved to `.console/setup/<env>/<stack>.tfvars.json` (outside git) and passed with `-var-file`; remote state is `-backend-config` derived from that cloud's own foundation inputs only (`<gcp_project_id>-tfstate`, `st<ado_org>tfstate`); form prefill never crosses clouds. Clouds are completely separate, as in production: never add a stack, variable group, vars file or JFrog integration shared by two clouds. The stack-setup pipelines take the same JSON as `inputs`/`state` parameters. Never write target values into `terraform/`. In production the targets already exist and only the two configure consoles run; Setup is never needed there.
- `portal/` — one UI for both consoles (`docs/merge-plan.md`): Serverless (`console/`) at `/serverless`, Kubernetes (`kcp_console/`) at `/k8s`. It only routes by prefix; the two consoles never import each other and keep separate stores, runs and gates.
- `kcp_console/` — the KCP console, served only by the portal at `/k8s` (no standalone entry point): the AKS kubelet credential provider sibling (spec `docs/kcp-console-spec.md`, progress `docs/kcp-progress.md`). Same rules as `console/`. State-changing az/kubectl/helm and JFrog writes run only as a direct plan (`kcp_console/direct.py`, AZURE.md Option B) or an ADO pipeline run, after a DryRun and an explicit, hash-bound approval. Ad-hoc reads stay read-only (`k8s.py`).

## Commands

```bash
# client tests
cd client && PYTHONPATH=src python -m unittest discover -s tests -v
# rotator tests
cd rotator && PYTHONPATH=../client/src:. python -m unittest test_rotate -v
# console tests (once console/ exists)
PYTHONPATH=client/src:. python -m unittest discover -s console/tests -v
# simulation scenarios vs expectations (M1+), including AWS and Knative
PYTHONPATH=client/src:rotator:. python -m console.sim check-all
PYTHONPATH=client/src:rotator:. python -m console.sim run happy-aws-lambda
# terraform (needs network to the provider registry)
terraform -chdir=terraform/stacks/all-targets init -backend=false && terraform -chdir=terraform/stacks/all-targets validate
terraform fmt -recursive -check terraform/
# run the console (M2+)
PYTHONPATH=client/src:rotator:. python -m console
# KCP console: tests, simulator, run (replay fixtures)
python3 -m unittest discover -s kcp_console/tests -t .
python3 -m kcp_console.sim check-all
PYTHONPATH=client/src:rotator:. python3 -m portal --kcp-fixtures kcp_console/tests/fixtures/sandbox
# portal: both consoles on one port, Serverless at /serverless, Kubernetes at /k8s
PYTHONPATH=client/src:rotator:. python3 -m unittest discover -s portal/tests -t .
PYTHONPATH=client/src:rotator:. python -m portal
# Setup console (test infrastructure): tests
PYTHONPATH=client/src:rotator:. python3 -m unittest discover -s setup_console/tests -t .
# KCP JFrog access: no stored token and nothing to set; the console reads its operator identity from gcp-foundation's console_operator_env output
```

## Rules

- **Stdlib only** in `client/`, `rotator/` and `console/`. No pip dependencies and no JS build step. The UI is vanilla ES modules with no CDN.
- **Never print, log, store or return a token.** That covers JFrog access tokens, identity JWTs, `JFROG_ACCESS_TOKEN`, Key Vault values and function keys. Route every log and SSE line through `console/redact.py`. If you add a log path, add it to the redaction test.
- **Simulation runs the real code.** Do not mock `jfrog_oidc` or `rotate.py` inside the simulator; emulate the HTTP endpoints they call. If the simulator needs a hook, add a seam to the real code (as `clock=`/`sleep=` already are) and keep the default behaviour identical.
- **No cloud or JFrog changes without approval.** `terraform apply`, `gcloud … update/deploy/execute`, `az … update/set/start` and JFrog writes happen only behind the console's approval gates. During development, do not run them yourself against real accounts unless I ask in the session.
- **Terraform is the source of truth** for identity, the JFrog side and the pull path. The console generates JSON-syntax workspaces (`main.tf.json`) that call the modules. Never string-template HCL.
- **Subprocesses:** argv lists only, never `shell=True`, always a timeout, and always through `console/shell.py`.
- **Server:** bind to 127.0.0.1, require the session token, check Host/Origin, set a strict CSP.
- **Tests first** for every step module. Use recorded fixtures for gcloud/az JSON and a fake `terraform` script in `console/tests/fixtures/`. Unit tests never need cloud access.

## Known unverified items

These come from the README's "Verify in your environment" section. Fix them in code when real providers or APIs disagree:

- The wire step's az flags: `az containerapp secret set --secrets name=keyvaultref:<url>,identityref:<id>` and `az containerapp registry set --password-secret-name`.
- The dedicated-identity attach flag `az containerapp [job] identity assign --user-assigned <id>` (`az functionapp identity assign --identities <id>` worked live on Flex, 2026-10-30).
- AWS (built from the AWS docs; account 365506438424 read-only so far: `get-outbound-web-identity-federation-info` returns `FeatureDisabled`, recorded as the Preflight fixture):
  - The JWT's `sub` for a workload's assumed-role session: the docs' example shows the role ARN; the mapping and the sim assume that. It may be the assumed-role ARN (`arn:aws:sts::<acct>:assumed-role/<role>/<session>`).
  - App Runner's instance-role credentials: assumed to be the container credentials endpoint (`AWS_CONTAINER_CREDENTIALS_*_URI`), and that App Runner accepts `JFROG_OIDC_AWS_REGION` (it reserves some `AWS_*` names).
  - Wire flags: `aws lambda update-function-configuration --environment <json> --role <arn>` then `aws lambda wait function-updated`; `aws ecs register-task-definition --cli-input-json` then `update-service --task-definition <family>` (family only = latest ACTIVE revision); `aws apprunner update-service --source-configuration/--instance-configuration`.
  - STS answers the Query API in XML; JSON (`Accept: application/json`) is not used.
  - IAM action names for the ado-setup policy (`iam:GetOutboundWebIdentityFederationInfo`) and the `aws_iam_outbound_web_identity_federation` resource (provider >= 6.26).
  - The ADO trust: IAM OIDC provider for the Entra v2.0 issuer with `client_id_list = [api://AzureADTokenExchange]` and `:sub` conditions.
- OpenShift / Knative: simulation only, no live cluster. Also unverified: Knative accepting a projected `serviceAccountToken` volume source in a ksvc (it may need a flag in the `config-features` ConfigMap), and the `kubernetes` state backend from an ADO agent.
- The Docker subdomain host form `<server>-<repo-key>.jfrog.io` on the target JFrog instance (it is what the AKS pilot's instance served; a self-hosted platform may differ: set `jfrog.docker_host`). On a JFrog Cloud trial (2026-10-30) it only redirected; the path form `<server>/<repo-key>/<image>` worked end to end with the KCP direct setup.

Verified live (2026-10-30): all eight workload types exchanged their identity for a JFrog token after console onboarding (docs/progress.md); Functions on ACA with `kind = functionapp`; the Container Apps log table `ContainerAppConsoleLogs_CL`; JFrog accepts `provider_type = "Azure"` (the `azure-entra` integration issued the Azure tokens).

Verified in M4 by `terraform validate` (azurerm 5.12.0): the `azurerm_function_app_flex_consumption` argument names. Verified by `terraform validate` (Terraform 1.15.5 then 1.17.1, jfrog/platform 2.2.11, google 8.9.0, azurerm 5.12.0) on both stacks: the `jfrog-oidc` module validations from the AKS pilot lessons. Verified live: the AR PATCH `updateMask` (gcloud sends the same mask).

Known blocker (2026-10-30, JFrog Cloud trial): Artifact Registry remote repos get 403 from the JFrog Cloud edge for any credentials (even anonymous); the requests never reach Artifactory's request log. GCP image pulls through AR need JFrog to allow that traffic. The runtime path (in-process exchange) works, and so does bringing images in at build time (OIDC token, pull from JFrog by repository path, push to AR), which avoids the AR remote. Proven with Cloud Build; CI/CD is Azure DevOps (for both Azure and GCP targets), so build and deploy pipelines are ADO only. Cloud Build and GitHub Actions are out of scope for now.
