# Verification status

## Known unverified items

These come from the README's "Verify in your environment" section. Fix them in code when real providers or APIs disagree:

- Settings sign-ins (`portal/connections.py`), parsed from CLI text and not yet run end to end against real accounts:
  - the gcloud `auth login --no-launch-browser --update-adc` prompt wording
  - the `az login --use-device-code` message format
  - whether Terraform's Google provider picks up `GOOGLE_APPLICATION_CREDENTIALS` from the console's gcloud dir
- The dedicated-identity attach flag `az containerapp [job] identity assign --user-assigned <id>` (`az functionapp identity assign --identities <id>` worked live, 2026-10-30).
- Cloud Run worker pools (`gcp_run_worker_pool`): only in `gcloud beta run worker-pools` (gcloud 555), so the ADO agent needs the beta commands; `describe`/`list` JSON read as v2 (`template.containers`, `template.serviceAccount`) or v1; `roles/run.developer` assumed to cover `run.workerPools.update`.
- OpenShift / Knative: simulation only, no live cluster. Also unverified: Knative accepting a projected `serviceAccountToken` volume source in a ksvc (it may need a flag in the `config-features` ConfigMap), and the `kubernetes` state backend from an ADO agent.
- azure-pipeline-identity finds each operator email's object ID through `azuread_users` by sign-in name (the email, or the guest form `<name>_<domain>#EXT#@<default domain>`); not applied yet.
- The Docker subdomain host form `<server>-<repo-key>.jfrog.io` on the target JFrog instance (it is what the AKS pilot's instance served; a self-hosted platform may differ: set `jfrog.docker_host`). On a JFrog Cloud trial (2026-10-30) it only redirected; the path form `<server>/<repo-key>/<image>` worked end to end with the KCP direct setup.

Verified live (2026-10-30): all eight workload types exchanged their identity for a JFrog token after console onboarding (docs/progress.md); Functions on ACA with `kind = functionapp`; the Container Apps log table `ContainerAppConsoleLogs_CL`; JFrog accepts `provider_type = "Azure"` (the `azure-entra` integration issued the Azure tokens).

Verified by `terraform validate` (Terraform 1.15.5 then 1.17.1, jfrog/platform 2.2.11, google 8.9.0, azurerm 5.12.0) on both stacks: the `jfrog-oidc` module validations from the AKS pilot lessons. Verified live: the AR PATCH `updateMask` (gcloud sends the same mask).

Resolved by design (2026-10-31): there is no AR pull path any more. Earlier finding (2026-10-30, JFrog Cloud trial): Artifact Registry remote repos got 403 from the JFrog Cloud edge for any credentials (even anonymous); the requests never reach Artifactory's request log. GCP images now come in at build time only. The runtime path (in-process exchange) works, and so does bringing images in at build time (OIDC token, pull from JFrog by repository path, push to AR), which avoids the AR remote. Proven with Cloud Build; CI/CD is Azure DevOps (for both Azure and GCP targets), so build and deploy pipelines are ADO only. Cloud Build and GitHub Actions are out of scope for now.
- Functions on Container Apps (2026-11-01): the function-key API (`az containerapp function keys list`, any key type, with or without `--revision`) fails with `UnexpectedException ... Failed to get or create ephemeral container`. The host itself runs and keeps its secrets in blob storage. The runtime exchange there is unverified until that API works or the endpoint is called another way.

Verified live on App Service (2026-11-04, `azure-web-app`, S1, `staging` slot, `swap=true`):
- Windows Node.js and Linux Node.js 24 LTS: `/healthz/jfrog` 200 on production and staging after the swap. On Windows, iisnode loads `server.js` through its interceptor, so `require.main !== module`: the app also starts when `IISNODE_VERSION` is set. IIS replaces the app's error bodies unless `web.config` has `<httpErrors existingResponse="PassThrough"/>`.
- `JFROG_*` app settings are not slot-sticky, so a swap moves them: onboarding wires production and every slot. It must be queued with the stack's `workspace` parameter, otherwise it logs "no azure workspace changed" and wires nothing.
- `az webapp deployment slot swap` can fail with a 409 from its own SwapSiteSlots status poll while the swap completes. `properties.slotSwapStatus` (ARM, `az rest`, api-version 2024-04-01; `az webapp show` leaves it empty) decides.
- Runtime defaults follow the supported list (`az webapp list-runtimes`, 2026-11-04): Node 24 LTS (20 is retired), PHP 8.4, Python 3.13, .NET 10 (8 ends 2026-11-10), Java 21.
