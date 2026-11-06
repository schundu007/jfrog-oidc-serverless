# JFrog OIDC for serverless

Short-lived JFrog tokens for Azure and Google Cloud serverless workloads, using OIDC token exchange. Every JFrog credential is minted at the moment it is needed and expires within hours. Nothing long-lived is stored anywhere: not in Terraform state, app settings, pipelines or variable groups. No registry username or password exists.

**Start here:** [docs/azure-deployment-guide.md](docs/azure-deployment-guide.md). Everything runs from Azure DevOps pipelines; the web console is optional.

## Scope and status

JFrog OIDC is to be enabled on every serverless and App Services workload type in Azure and Google Cloud. The proof of concept is complete on the App Service Web App; every other type is implemented and its proof of concept is pending.

### App Services (web apps)

| Cloud | Workload type | Stack | How code and packages get in | Runtime JFrog access | Status |
|---|---|---|---|---|---|
| Azure | **App Service Web App** (Linux or Windows; Python, Java, .NET, Node.js, PHP; slots and swap) | `azure-web-app` | Code deploy (zip/jar). pip, Maven, NuGet, npm and Composer resolve through JFrog with the pipeline's OIDC token | Managed identity → JFrog token | **POC done** |
| Google Cloud | **App Engine standard** (Python 3.12; the Web App equivalent) | `gcp-app-engine` | Code deploy from source. pip resolves through JFrog with the pipeline's OIDC token (wheels vendored, `--no-index`) | Its own service account's identity token → JFrog token | POC pending |

### Serverless

| Cloud | Workload type | Stack | How code and packages get in | Runtime JFrog access | Status |
|---|---|---|---|---|---|
| Azure | Container App | `azure-container-app` | Pipeline pulls the image from JFrog with OIDC, pushes to the stack's own ACR | Managed identity → JFrog token | POC pending |
| Azure | Container Apps job | `azure-container-apps` | Same as Container App | Same | POC pending |
| Azure | Functions on Container Apps | `azure-functions-aca` | Same as Container App | Same | POC pending |
| Azure | Functions Premium / Dedicated | `azure-functions-premium` | Zip deploy; packages through JFrog | Same | POC pending |
| Azure | Functions Flex Consumption | `azure-functions-flex` | Zip deploy; packages through JFrog | Same | POC pending |
| Google Cloud | Cloud Run job / service / function / worker pool | `gcp-cloud-run`, `gcp-cloud-run-service`, `gcp-cloud-run-function`, `gcp-cloud-run-worker-pool` | Pipeline pulls from JFrog with OIDC, pushes to the stack's own Artifact Registry | Service account identity token → JFrog token | POC pending |

Each workload type is its own stack, its own resource group (or project service), identity and Terraform state, and its own pair of pipelines. Deploying one never touches another.

## How it works

```
RUNTIME (your code calls Artifactory)
  workload ──platform identity token──▶ JFrog /access/api/v1/oidc/token ──▶ 1h access token
  (managed identity / service account)   cached in-process, refreshed 5 min before expiry

BUILD (every cloud, Azure DevOps)
  build-deploy pipeline ──service connection OIDC token──▶ JFrog: images and packages
                        ──▶ push to the stack's own registry (ACR / Artifact Registry), or deploy code (zip, App Engine)
                        ──▶ the platform pulls with its own identity
```

## Order of work (all in Azure DevOps)

| # | Pipeline | Creates |
|---|---|---|
| 1 | Azure - Register pipelines | Registers every other pipeline (created once in the ADO UI) |
| 2 | Pipeline identity and state | State storage, build/setup identities, service connections `jfrog-oidc-azure-build`/`-setup`, variable group, approval environments |
| – | JFrog UI, once | The OIDC integration `ado-<org>-azure` and its `ado-setup` mapping, which step 3 imports |
| 3 | Pipelines' JFrog trust | The `ado-build` mapping, reader group, remote repositories and the JFrog variable group |
| 4 | `<stack>` - Infrastructure | One workload type's resources (plan → approval → apply) |
| 5 | `<stack>` - Build and deploy | Builds through JFrog and rolls out the workload |
| 6 | Azure - Onboard workloads to JFrog | The workload identity's JFrog mapping and its `JFROG_*` app settings |

Every value that names your environment (subscription, location, workload name, JFrog URL, people) is entered when you run the pipeline or in the web console. Nothing in this repo names an account. Details: [docs/azure-deployment-guide.md](docs/azure-deployment-guide.md).

## Layout

```
client/              jfrog_oidc: stdlib-only Python client (identity sources, exchange + cache, CLI: claims, check, token, docker-login,
                     pip, mvn, gradle, dotnet, npm, composer, helm, jf)
apps/                sample workloads: web-app (python via cloud-run-service, java, dotnet, node, php), azure-function,
                     artifact-reader (jobs), cloud-run-service (also App Engine: web-app/app-engine/app.yaml),
                     cloud-run-function, cloud-run-worker-pool
terraform/modules/   jfrog-oidc (integrations, mappings), azure-identity (Entra audience app), azure-workload
terraform/stacks/    serverless/<cloud>/<stack>, app-services/<cloud>/<stack>, oidc/<cloud>/<stack>
pipelines/           Azure DevOps: one folder per stack (stack-setup.yml, build-deploy.yml), templates, scripts, vars
portal/              optional web UI (python -m portal): Serverless, App Services, Infrastructure, JFrog OIDC, Settings
console/ setup_console/   the UI's consoles: configure, simulate, verify, deploy
```

## How trust is pinned

**Azure.** Workloads request a token for `api://<audience-app-id>` (v2 tokens: `iss=https://login.microsoftonline.com/<tenant>/v2.0`). Each mapping requires `oid`, `tid` and `aud`, and the audience app requires an app role assignment, so only listed identities can get a token at all.

**Google Cloud.** Issuer `https://accounts.google.com` (Cloud Run and App Engine); each mapping requires the service account's numeric unique ID (`sub`, never reused) and `aud`.

**Pipelines.** ADO service connections use workload identity federation; their OIDC token is exchanged with JFrog through the `ado-build` and `ado-setup` mappings.

**Tokens.** Runtime mappings issue group-scoped tokens limited to Artifactory. Use read-only groups. JFrog caps mapping token lifetime at 24h; the default here is 1h.

**App Engine settings.** App Engine standard sets environment variables only with a deploy (`app.yaml` `env_variables`), never in place. Its build-deploy pipeline therefore writes the `JFROG_*` settings (URL, provider, audience, source; no secret) into `app.yaml`, carrying over the serving version's values. Onboarding creates the mapping and reports the settings as "set at the next build-deploy".

## Verify a workload

From inside the workload (Web App SSH/Kudu console, `az containerapp exec`, Cloud Run job shell, App Engine logs):

- `python -m jfrog_oidc claims`: the platform token's claims; compare them with the mapping.
- `python -m jfrog_oidc check`: performs the exchange and prints the username and expiry, never the token.
- Web apps (App Service, App Engine, Cloud Run services) answer `/healthz/jfrog` with the same check.

## Operations

- **`401` from the exchange:** compare `python -m jfrog_oidc claims` with the mapping. On Azure, `ver: "1.0"` means the audience app issues v1 tokens; fix `requestedAccessTokenVersion`.
- **JFrog outage:** images already sit in ACR/Artifact Registry, so running apps and scale-out keep working; cached tokens stay valid until they expire. New builds wait.
- **Revoking a workload:** delete its mapping. Issued tokens live until their TTL (≤ 1h).

What has and has not been verified live: [docs/verification-status.md](docs/verification-status.md).

## Tests

```bash
cd client && PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=client/src:. python -m unittest discover -s console/tests
PYTHONPATH=client/src:. python3 -m unittest discover -s setup_console/tests -t .
PYTHONPATH=client/src:. python3 -m unittest discover -s portal/tests -t .
PYTHONPATH=client/src:. python -m console.sim check-all
```
