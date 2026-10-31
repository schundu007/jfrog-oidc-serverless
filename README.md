# JFrog short-lived tokens for Cloud Run, Azure Container Apps and Azure Functions

Every JFrog credential in this setup is minted by OIDC token exchange and expires within hours. Nothing long-lived is stored anywhere: not in Terraform state, app settings or CI.

Serverless platforms touch JFrog at two different moments, so there are two paths.

```
RUNTIME PATH (your code calls Artifactory)
  workload ──platform ID token──▶ JFrog /access/api/v1/oidc/token ──▶ 1h access token
  (Cloud Run SA / managed identity)      cached in-process, refreshed 5 min before expiry

PULL PATH (platform pulls image/packages before your code runs)
  scheduler ─▶ rotator job ──its own ID token──▶ JFrog ──▶ 4h read-only token
                    │
                    ├─ GCP:   new Secret Manager version ─▶ AR remote repos repointed
                    └─ Azure: Key Vault secret (versionless) ─▶ Container Apps registry
                                                              └▶ Functions DOCKER_REGISTRY_SERVER_PASSWORD
```

The pull path exists because platform image pulls only accept a username and password; they cannot call JFrog's exchange themselves. The rotator makes that password a short-lived token minted from a workload identity.

## What each platform uses

| Platform | Image pull | Build-time packages | Runtime calls |
|---|---|---|---|
| Cloud Run function | Built image lives in AR (Google-managed) | `requirements.txt` → AR remote repo → JFrog (rotated token) | `jfrog_oidc` client, SA identity token |
| Cloud Run service/job from a JFrog image | AR Docker remote repo → JFrog (rotated token) | n/a | `jfrog_oidc` client |
| Container App | Registry `passwordSecretRef` → Key Vault (rotated token) | Build in CI with CI OIDC → JFrog | `jfrog_oidc` client, managed identity |
| Functions on Container Apps | Same as Container App | Same | Same |
| Functions Premium/Dedicated (custom container) | `DOCKER_REGISTRY_SERVER_PASSWORD` = Key Vault reference, refreshed by rotator | Same | Same |
| Functions Flex Consumption (code) | None | Build in CI, deploy with remote build off | Same |

## Layout

```
client/                           jfrog_oidc: stdlib-only Python client (tests: 20)
  src/jfrog_oidc/                 sources (GCP metadata, Azure MI, file), exchange + cache, helpers, CLI
rotator/                          pull-path rotator for GCP and Azure (tests: 5) + Dockerfile
terraform/modules/
  jfrog-oidc/                     OIDC integrations + per-workload identity mappings
  gcp-pull-path/                  AR remote repos, secret, rotator job, scheduler, alert
  azure-identity-and-pull-path/   Entra audience app, rotator identity/job, Key Vault wiring, alert
terraform/stacks/all-targets/     both clouds end to end, incl. all 3 Functions hosting models
apps/                             Cloud Run function and Azure Function using the client
```

## Docker registry host

Images are pulled from the JFrog Docker repo's **own host**, `<server>-<repo-key>.jfrog.io` (for example `acme-docker-virtual.jfrog.io/orders:1.4.2`). Do not use `<server>.jfrog.io/<repo-key>/...`: that repository-path method failed for local repos on the AKS pilot's instance. Set `jfrog_docker_repo` (or `jfrog_docker_host` to override the derived name) in the stack. In the console, the same fields are `jfrog.docker_repo` and `jfrog.docker_host`. Each AR Docker remote proxies one JFrog repo through that host. See `docs/aks-pilot-lessons.md`.

`jfrog_url` is always the platform origin (`https://acme.jfrog.io`), with no path. The client, console and modules reject anything else.

## How trust is pinned

**GCP.** The integration trusts issuer `https://accounts.google.com`. Each mapping requires `sub` (the service account's numeric unique ID, which is never reused) and `aud` (`jfrog-gcp`). A deleted and recreated SA with the same email does not match.

**Azure.** Workloads request a token for `api://<audience-app-id>`. That app has `requestedAccessTokenVersion = 2`, so tokens carry `iss=https://login.microsoftonline.com/<tenant>/v2.0`, `aud=<app-id>` and `oid=sub=<identity principal ID>`. Each mapping requires `oid`, `tid` and `aud`. By default the audience app also requires an app role assignment, so only identities you list can get a token for it in the first place.

**Tokens.** Runtime mappings issue purely group-scoped tokens (`applied-permissions/groups:"…"`) limited to Artifactory (`jfrt@*`). Pull mappings also need a fixed username, because platform registry settings require one. JFrog then adds that user's own permissions and Auto Join groups to the token, so create those users with nothing beyond read. Use read-only groups for every mapping here. JFrog caps mapping token lifetime at 24h.

## Deploy

1. **Publish the client** to your Artifactory PyPI repo (`python -m build client/`, then `jf rt upload`), or vendor `client/src/jfrog_oidc` into each app.
2. **Build the rotator image** from the repo root and push it to a *standard* AR repo and to ACR: `docker build -f rotator/Dockerfile -t <registry>/jfrog-oidc-rotator:0.1.0 .` Add `--build-arg BASE_IMAGE=<approved mirror>/python:3.12-slim` where policy forbids pulling base images from Docker Hub. Do not host it behind the JFrog remote it refreshes. If the token ever lapses, the rotator must still start to fix it.
3. **Apply Terraform** from `terraform/stacks/all-targets` (copy `terraform.tfvars.example`). The JFrog provider needs an admin token in `JFROG_ACCESS_TOKEN`; in CI, get it with `jf eot` from your pipeline's own OIDC rather than storing one.
4. **Apply in two stages, then run each rotator once.** The repos and the Key Vault secret are created with a placeholder, because AR and Key Vault need a value at create time. Any workload that pulls through them fails until the first rotation, so a single apply that also creates the workloads cannot succeed. Stage one creates the identities, the JFrog side and the pull path: `terraform apply -target=module.jfrog_oidc -target=module.gcp -target=module.azure`. Then run both `terraform output first_run` commands. Stage two is a plain `terraform apply`, which creates the workloads. The console's pipeline does the same thing: rotation runs before wiring.
5. **Verify** from inside a workload (Cloud Run job shell, `az containerapp exec`):
   - `python -m jfrog_oidc claims` shows the platform token's claims; compare them with the mapping.
   - `python -m jfrog_oidc check` performs the exchange and prints the username and expiry, never the token.
   - Pull an uncached image through `…-docker.pkg.dev/<p>/jfrog-docker/<image>:<tag>`. A cached image proves nothing about authentication.
   - In Artifactory's request/audit log, confirm the pull is attributed to the pull mapping's username (`svc-gcp-ar-upstream`, `svc-azure-registry-pull`) and not to a static account. This was the AKS pilot's last open item; do it here before calling the pull path done.

## TTL vs rotation interval

| Consumer | Picks up a new token | Defaults | Survives |
|---|---|---|---|
| AR remote repo | Immediately (repo repointed to the new version) | rotate hourly, 4h TTL | 2 missed runs |
| Container Apps registry | ≤ 30 min (versionless Key Vault ref) | rotate hourly, 4h TTL | ~2 missed runs |
| Functions Premium/Dedicated | Immediately via refresh API; ≤ 24h without it | rotate hourly, 4h TTL | Refresh must succeed; the run fails if it doesn't. The refresh permission is required: JFrog caps tokens at 24h, so no TTL can outlast the 24h reference cache. |
| Runtime client | Refreshes 5 min before expiry (clamped to ⅓ of TTL) | 1h TTL | Serves the cached token while JFrog is briefly unreachable |

The rotator refuses to write a token with less than `MIN_TTL_SECONDS` (default 30 min) left. That floor only rejects tokens that are nearly useless; it does not prove the TTL is long enough. A pull TTL must cover at least two rotation intervals plus the platform's pickup lag (the table above); the console's validation warns when it doesn't, and `python -m console.sim` shows the effect.

## Operations

- **Alerting.** Both modules alert on the *absence* of a successful rotation (default: 3h), before the 4h token can expire. Wire `notification_channels` (GCP) and `alert` (Azure).
- **Rotator failing.** Check the job logs. Each run emits one JSON line per step: `secret_version_added`, `repository_repointed`, `keyvault_secret_set`, `function_app_refreshed`, `rotation_failed`. Re-run it manually with the `first_run` commands.
- **`401` from the exchange.** Run `python -m jfrog_oidc claims` and compare `iss`/`aud`/`sub` or `oid`/`tid`/`aud` with the integration and the mapping's claims JSON. On Azure, `ver: "1.0"` means the audience app is issuing v1 tokens (`iss=https://sts.windows.net/...`); fix `requestedAccessTokenVersion`.
- **JFrog outage.** AR serves cached artifacts. Warm runtime instances keep using their cached token until it expires. Container Apps replicas that are already running are unaffected; new replicas need JFrog for uncached pulls.
- **Revoking a workload.** Delete its identity mapping. Tokens already issued live until their TTL (≤ 4h); revoke them in JFrog if needed.

## Verify in your environment before production

The Terraform here is written against the providers' and platforms' documented interfaces. It has been syntax-checked and cross-reference-checked, but not `terraform validate`d against downloaded providers or applied. The Python client and rotator have unit tests against fakes of every endpoint they call. Check these points on a non-prod project, subscription and JFrog project first:

1. **JFrog Azure provider type.** I set the Azure integration to `provider_type = "Azure"` with the v2.0 issuer. If JFrog rejects the token, switch `azure.provider_type` to `"generic"`; the issuer and claims stay the same.
2. **AR update mask.** The rotator PATCHes with the leaf masks the Google provider itself uses (`…usernamePasswordCredentials.username` and `…usernamePasswordCredentials.passwordSecretVersion`). Override with `AR_UPDATE_MASK` on the job if needed.
3. **Refresh role action name.** The rotator's custom role uses `Microsoft.Web/sites/config/configreferences/appsettings/refresh/action`. Confirm it with `az provider operation show --namespace Microsoft.Web` and set `function_refresh_actions` if your tenant lists it differently.
4. **Key Vault reference in `DOCKER_REGISTRY_SERVER_PASSWORD`.** Confirm a cold start pulls the image on a Premium plan after a rotation.
5. **Functions on Container Apps.** `functions_on_aca_api_version` must be a `Microsoft.App` API version that accepts `kind = "functionapp"` in your tenant.
6. **Azure alert query.** The default KQL uses `ContainerAppConsoleLogs_CL`. Environments using diagnostic settings write to `ContainerAppConsoleLogs` instead; override `alert.query`.
7. **`azurerm_function_app_flex_consumption` arguments.** These are recent; check them against the azurerm version you pin.
8. **Cloud Run functions builds.** The `all-targets` stack grants the default compute SA (the default build SA on newer projects) read access on the AR repos. If you use a custom build service account, grant that one instead.

## Tests

```
cd client  && PYTHONPATH=src python -m unittest discover -s tests -v
cd rotator && PYTHONPATH=../client/src:. python -m unittest test_rotate -v
```
