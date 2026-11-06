# JFrog short-lived tokens for Cloud Run, Azure Container Apps and Azure Functions

Every JFrog credential in this setup is minted by OIDC token exchange and expires within hours. Nothing long-lived is stored anywhere: not in Terraform state, app settings or CI.

Serverless platforms touch JFrog at two moments: when the image is built and when the code runs. Both use OIDC; there is no registry username or password anywhere.

```
RUNTIME PATH (your code calls Artifactory)
  workload ──platform ID token──▶ JFrog /access/api/v1/oidc/token ──▶ 1h access token
  (Cloud Run SA / managed identity)      cached in-process, refreshed 5 min before expiry

IMAGE PATH (build time, every cloud)
  ADO build-deploy ──service connection OIDC──▶ JFrog: pull image / packages
                   ──▶ push to the stack's own registry (AR on GCP, ACR on Azure)
                   ──▶ platform pulls with its own identity
```

Platform registry settings (AR remote repos, Container Apps registries) only take a username and password, so nothing points them at JFrog. The pipeline brings the image in at build time with its OIDC token instead.

## What each platform uses

| Platform | Image pull | Build-time packages | Runtime calls |
|---|---|---|---|
| Cloud Run function | Built image lives in AR (Google-managed) | Pipeline vendors the client; packages from JFrog with CI OIDC | `jfrog_oidc` client, SA identity token |
| Cloud Run service/job | Stack's AR; CI fills it from JFrog with OIDC | Build in CI with CI OIDC → JFrog | `jfrog_oidc` client |
| Container App | Own ACR via managed identity; CI fills it from JFrog with OIDC | Build in CI with CI OIDC → JFrog | `jfrog_oidc` client, managed identity |
| Functions on Container Apps | Same as Container App | Same | Same |
| Functions Premium/Dedicated (zip) | None (code deploy) | Build in CI with CI OIDC → JFrog | Same |

## Layout

```
client/                           jfrog_oidc: stdlib-only Python client (tests: 20)
  src/jfrog_oidc/                 sources (GCP metadata, Azure MI, file), exchange + cache, helpers, CLI
terraform/modules/
  jfrog-oidc/                     OIDC integrations + per-workload identity mappings
  azure-identity/                 Entra audience app
terraform/stacks/<type>/          one stack per workload type (Infrastructure page), each on its own
apps/                             Cloud Run function and Azure Function using the client
```

## Docker registry host

Images are pulled from the JFrog Docker repo's **own host**, `<server>-<repo-key>.jfrog.io` (for example `acme-docker-virtual.jfrog.io/orders:1.4.2`). Do not use `<server>.jfrog.io/<repo-key>/...`: that repository-path method failed for local repos on the AKS pilot's instance.

`jfrog_url` is always the platform origin (`https://acme.jfrog.io`), with no path. The client, console and modules reject anything else.

## How trust is pinned

**GCP.** The integration trusts issuer `https://accounts.google.com`. Each mapping requires `sub` (the service account's numeric unique ID, which is never reused) and `aud` (`jfrog-gcp`). A deleted and recreated SA with the same email does not match.

**Azure.** Workloads request a token for `api://<audience-app-id>`. That app has `requestedAccessTokenVersion = 2`, so tokens carry `iss=https://login.microsoftonline.com/<tenant>/v2.0`, `aud=<app-id>` and `oid=sub=<identity principal ID>`. Each mapping requires `oid`, `tid` and `aud`. By default the audience app also requires an app role assignment, so only identities you list can get a token for it in the first place.

**Tokens.** Runtime mappings issue purely group-scoped tokens (`applied-permissions/groups:"…"`) limited to Artifactory (`jfrt@*`). Only the JFrog trust stacks' setup and console-operator mappings carry a username. Use read-only groups for every mapping here. JFrog caps mapping token lifetime at 24h.

## Deploy

Start here, everything runs from Azure DevOps: [docs/fresh-start-azure.md](docs/fresh-start-azure.md).


1. **Publish the client** to your Artifactory PyPI repo (`python -m build client/`, then `jf rt upload`), or vendor `client/src/jfrog_oidc` into each app.
2. **Deploy each workload type on its own.** On the Infrastructure page (Setup), deploy the cloud's pipeline identity, then only the workload-type stacks you need, one at a time: Cloud Run job, service or function; Container Apps job, Container App, Functions on Container Apps, or Functions Premium / Dedicated. Each has its own identity and resources and never touches another. Roll each out with its cloud's build-deploy pipeline (one `kind` and `workload` per run).
3. **Onboard each workload** in the Serverless console: it writes the JFrog side and the workload's JFROG_* settings, per workload.
4. **Verify** from inside a workload (Cloud Run job shell, `az containerapp exec`):
   - `python -m jfrog_oidc claims` shows the platform token's claims; compare them with the mapping.
   - `python -m jfrog_oidc check` performs the exchange and prints the username and expiry, never the token.
   - In Artifactory's request log, confirm the pipeline's image pull is attributed to its OIDC mapping, not to a static account.

## Token lifetime

| Consumer | Picks up a new token | Defaults | Survives |
|---|---|---|---|
| Runtime client | Refreshes 5 min before expiry (clamped to ⅓ of TTL) | 1h TTL | Serves the cached token while JFrog is briefly unreachable |
| Build pipeline | Exchanges once per run | 1h TTL | n/a |

## Operations

- **`401` from the exchange.** Run `python -m jfrog_oidc claims` and compare `iss`/`aud`/`sub` or `oid`/`tid`/`aud` with the integration and the mapping's claims JSON. On Azure, `ver: "1.0"` means the audience app is issuing v1 tokens (`iss=https://sts.windows.net/...`); fix `requestedAccessTokenVersion`.
- **JFrog outage.** Images already sit in AR/ACR, so deploys and scale-out keep working. Warm runtime instances keep using their cached token until it expires. New builds wait until JFrog is back.
- **Revoking a workload.** Delete its identity mapping. Tokens already issued live until their TTL (≤ 1h); revoke them in JFrog if needed.

## Verify in your environment before production

The Terraform here is written against the providers' and platforms' documented interfaces. It has been syntax-checked and cross-reference-checked, but not `terraform validate`d against downloaded providers or applied. The Python client has unit tests against fakes of every endpoint they call. Check these points on a non-prod project, subscription and JFrog project first:

1. **JFrog Azure provider type.** I set the Azure integration to `provider_type = "Azure"` with the v2.0 issuer. If JFrog rejects the token, switch `azure.provider_type` to `"generic"`; the issuer and claims stay the same.
2. **Functions on Container Apps.** `functions_on_aca_api_version` must be a `Microsoft.App` API version that accepts `kind = "functionapp"` in your tenant.
3. **Cloud Run functions builds.** The `gcp-cloud-run-function` stack grants the default compute SA (the default build SA on newer projects) read access on its source bucket. If you use a custom build service account, grant that one instead.

## Tests

```
cd client  && PYTHONPATH=src python -m unittest discover -s tests -v
```
