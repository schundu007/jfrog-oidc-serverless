# Verification status

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
- Cloud Run worker pools (`gcp_run_worker_pool`): only in `gcloud beta run worker-pools` (gcloud 555), so the ADO agent needs the beta commands; `describe`/`list` JSON read as v2 (`template.containers`, `template.serviceAccount`) or v1; `roles/run.developer` assumed to cover `run.workerPools.update`.
- OpenShift / Knative: simulation only, no live cluster. Also unverified: Knative accepting a projected `serviceAccountToken` volume source in a ksvc (it may need a flag in the `config-features` ConfigMap), and the `kubernetes` state backend from an ADO agent.
- azure-foundation finds each operator email's object ID through `azuread_users` by sign-in name (the email, or the guest form `<name>_<domain>#EXT#@<default domain>`); not applied yet.
- The Docker subdomain host form `<server>-<repo-key>.jfrog.io` on the target JFrog instance (it is what the AKS pilot's instance served; a self-hosted platform may differ: set `jfrog.docker_host`). On a JFrog Cloud trial (2026-10-30) it only redirected; the path form `<server>/<repo-key>/<image>` worked end to end with the KCP direct setup.

Verified live (2026-10-30): all eight workload types exchanged their identity for a JFrog token after console onboarding (docs/progress.md); Functions on ACA with `kind = functionapp`; the Container Apps log table `ContainerAppConsoleLogs_CL`; JFrog accepts `provider_type = "Azure"` (the `azure-entra` integration issued the Azure tokens).

Verified in M4 by `terraform validate` (azurerm 5.12.0): the `azurerm_function_app_flex_consumption` argument names. Verified by `terraform validate` (Terraform 1.15.5 then 1.17.1, jfrog/platform 2.2.11, google 8.9.0, azurerm 5.12.0) on both stacks: the `jfrog-oidc` module validations from the AKS pilot lessons. Verified live: the AR PATCH `updateMask` (gcloud sends the same mask).

Known blocker (2026-10-30, JFrog Cloud trial): Artifact Registry remote repos get 403 from the JFrog Cloud edge for any credentials (even anonymous); the requests never reach Artifactory's request log. GCP image pulls through AR need JFrog to allow that traffic. The runtime path (in-process exchange) works, and so does bringing images in at build time (OIDC token, pull from JFrog by repository path, push to AR), which avoids the AR remote. Proven with Cloud Build; CI/CD is Azure DevOps (for both Azure and GCP targets), so build and deploy pipelines are ADO only. Cloud Build and GitHub Actions are out of scope for now.
