# Verification status

What has been tested against real Azure, Google Cloud and JFrog accounts, and what has not. JFrog OIDC is to be enabled on every serverless and App Services workload type in Azure and Google Cloud; the proof of concept on the current Azure DevOps pipeline flow is complete on the App Service Web App and pending for every other type. When a provider behaves differently from what is described here, fix the code and update this page.

## Verified

| Area | What was tested |
|---|---|
| Runtime token exchange (earlier test environment, before the current pipeline flow) | Each of these exchanged its own identity for a 1 h JFrog token after onboarding: Cloud Run job, service and function (gen2); Container App; Container Apps job; Functions on Container Apps, Premium and Flex Consumption |
| App Service Web Apps (POC done) | Node.js 24 LTS on Linux and on Windows answers `/healthz/jfrog` with 200 on the production slot and on the `staging` slot, before and after a swap |
| Functions on Container Apps | Runs with `kind = functionapp` |
| Functions identity attach | `az functionapp identity assign --identities <id>` |
| Container Apps logs | Read from the `ContainerAppConsoleLogs_CL` table |
| JFrog integration type | JFrog accepts `provider_type = "Azure"` for Entra-issued tokens |
| Images at build time | The pipeline gets a JFrog token with OIDC, pulls the image by repository path and pushes it to the stack's own registry |
| JFrog Docker host | The path form `<server>/<repo-key>/<image>` works end to end |
| Artifact Registry | The repository `PATCH` with `updateMask` |
| Terraform | `terraform validate` passes on every stack (Terraform 1.17, jfrog/platform 2.2, google 8.9, azurerm 5.12) |

## Not yet verified

| Area | Open question |
|---|---|
| Web console sign-ins (`portal/connections.py`) | The `gcloud auth login --no-launch-browser --update-adc` prompt text and the `az login --use-device-code` message format are parsed from CLI output; Terraform's Google provider picking up `GOOGLE_APPLICATION_CREDENTIALS` from the console's gcloud directory |
| Container Apps identity attach | `az containerapp [job] identity assign --user-assigned <id>` |
| POC on the current pipeline flow | Every workload type except the Web App: Container App, Container Apps job, Functions on Container Apps, Premium and Flex Consumption; Cloud Run job, service, function and worker pool; App Engine standard |
| App Service: Python, Java, .NET, PHP | Same flow as Node.js; not yet run live |
| Cloud Run worker pools | Only available in `gcloud beta run worker-pools`, so the agent needs the beta component; `describe`/`list` JSON is read as v2 (`template.containers`, `template.serviceAccount`) or v1; `roles/run.developer` is assumed to cover `run.workerPools.update` |
| App Engine standard (`gcp-app-engine`) | Not deployed yet. Open: the `gcloud app services describe` (`split.allocations`) and `gcloud app versions describe` (`envVariables`, `serviceAccount`, `versionUrl`) JSON shapes; the metadata server's ID token for the version's own service account; `gcloud app deploy --service-account`; pip installing only from vendored wheels (`--no-index --find-links wheels`) in App Engine's build; whether `roles/appengine.deployer`, `appengine.serviceAdmin`, `cloudbuild.builds.editor` and `storage.objectAdmin` on the build identity are enough |
| Pipeline identity operators | `azure-pipeline-identity` resolves each operator email to an object ID through `azuread_users` (the email, or the guest form `<name>_<domain>#EXT#@<default domain>`) |
| JFrog Docker subdomain host | The form `<server>-<repo-key>.jfrog.io` depends on the JFrog instance; when it does not work, set `jfrog.docker_host` or use the path form |
| Functions on Container Apps keys | `az containerapp function keys list` fails with `Failed to get or create ephemeral container`, so a fresh deployment cannot read its function key to call `/api/healthz/jfrog` |

## Known behaviour

| Area | Behaviour and how the code handles it |
|---|---|
| Artifact Registry remote repositories | Requests from Artifact Registry to JFrog Cloud are refused at the JFrog edge. There is no registry pull path: images come in at build time only |
| Windows App Service (Node.js) | iisnode loads `server.js` through its interceptor, so `require.main !== module`; the app also starts when `IISNODE_VERSION` is set. `web.config` needs `<httpErrors existingResponse="PassThrough"/>` or IIS replaces the app's error bodies |
| App Service slot swap | `JFROG_*` app settings are not slot-sticky and move with a swap, so onboarding wires production and every slot. Queue onboarding with the stack's `workspace` parameter, otherwise it wires nothing |
| `az webapp deployment slot swap` | Can return 409 from its own status poll while the swap completes; the pipeline reads `properties.slotSwapStatus` through ARM (`az rest`, api-version 2024-04-01) to decide |
| App Service runtimes | Defaults follow `az webapp list-runtimes`: Node.js 24 LTS, PHP 8.4, Python 3.13, .NET 10, Java 21 |
