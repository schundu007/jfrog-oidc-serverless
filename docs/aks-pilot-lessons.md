# Lessons from the AKS sandbox pilot, applied to serverless

The AKS pilot (JFrog kubelet credential provider 1.4.0, Option B: the cluster's
projected ServiceAccount token exchanged directly at Artifactory Access) pulled
`example-aks-sandbox-test-docker-local.jfrog.io/mcp/sonarqube:1.26-v1` with
`imagePullSecrets: []` on 2026-10-16. This repo does the same thing for
platforms that have no kubelet. The trust model is the same: a platform-signed
identity token goes to `/access/api/v1/oidc/token`, gets validated against an
OIDC integration, matches an exact identity mapping, and comes back as a
short-lived token. Only where the token is minted differs.

| AKS (kubelet plugin, Option B) | Serverless (this repo) |
|---|---|
| Projected SA token, `iss` = the cluster's OIDC issuer | GCP: Google ID token, `iss=https://accounts.google.com`, `sub` = SA unique ID. Azure: Entra v2 token for the audience app, `oid`/`tid`/`aud` |
| One integration per cluster (`aks-sandbox-oidc`) | One per cloud issuer (`gcp-cloudrun`, `azure-entra` per tenant) |
| Mapping `sub: system:serviceaccount:jfrog:jfrog-provider-sa` | One mapping per workload identity (`modules/jfrog-oidc`) |
| kubelet calls the plugin at pull time; result cached `defaultCacheDuration` | Pull path: the rotator writes a 4 h token into Secret Manager / Key Vault every hour. Runtime: the in-process client refreshes 5 min before expiry |
| Plugin binary mirrored into `aks-sandbox-generic-local` (TLS interception) | Rotator image hosted in AR/ACR; base image overridable (`BASE_IMAGE`) |

The Azure side here uses an Entra app registration as the token audience, so it
is the AAD-backed shape (closer to AZURE.md's Option A). That matches the
workforce IAM standard's AAD preference, which the pilot's security review
flagged as an open deviation for Option B on AKS.

## What changed in this repo because of the pilot

1. **Docker pulls use the repo's dedicated host.** On the pilot's instance, the
   repository-path method (`example.jfrog.io/<repo-key>/<image>`) failed for local
   repos with `Unsupported v2 repository request for ''`, and the token realm
   came back without the repo key. Every Docker repo answers on
   `<server>-<repo-key>.jfrog.io` instead.
   - Container Apps / Functions on ACA: `registry.server` and the image are on
     that host (`terraform/examples/complete/azure.tf`, console wire step).
   - Functions Premium: `DOCKER_REGISTRY_SERVER_URL` and `image_name` likewise.
   - GCP: each AR Docker remote proxies one JFrog repo through its own host, so
     images resolve as `<ar-repo>/<image>`. The module rejects a Docker
     `upstream_uri` with a path. The console warns when it is the platform host.
   - Console: `jfrog.docker_repo` derives the host; `jfrog.docker_host`
     overrides it (self-hosted platforms).
2. **The JFrog URL is an origin, checked everywhere.** On AKS, a scheme the
   plugin also prepends produced `https://https://example.jfrog.io/...`. That
   failed DNS before any exchange, and the error showed only in the plugin's
   own log; containerd just reported an anonymous 401. Here the convention is
   the opposite (a full `https://host`; the code appends paths), so the trap is
   a path such as `/artifactory`. Every layer now refuses it: the client,
   rotator (same client), console validation, preflight, tfgen and each
   Terraform module's `jfrog_url` variable.
3. **An integration name is never reused across issuers.** The pilot lost
   hours to `aks-other-cluster-oidc`, a real integration for a different
   cluster that a loose pipeline variable pointed at. Preflight now fails, not
   warns, when the configured integration name exists with a different
   `issuer_url`.
4. **Mapping rules.** Each subject is exact (no wildcards), there is one
   mapping per identity, and runtime tokens last at most 3600 s. This is
   enforced in `modules/jfrog-oidc` and the console: model validation for the
   TTL, tfgen for two targets that share an identity. Pull (rotator) mappings
   keep their 4 h TTL, which the rotation math needs.
5. **Settings are committed, not loose.** The console stores every
   security-relevant value in the saved config and the generated tfvars. Only
   `JFROG_ACCESS_TOKEN` comes from the environment.
6. **Base image sourcing.** The pilot's review flagged images pulled from
   outside the approved artifact repository. `rotator/Dockerfile` takes
   `--build-arg BASE_IMAGE=<approved mirror>`.

## Checks the pilot taught that still need doing per environment

- **Audit attribution.** On AKS, the Artifactory audit-log check (that the pull
  is attributed to `kcp-pull-user`) was still open. Do the same here:
  - Pull path: the pull should be attributed to the mapping's `username`
    (`svc-gcp-ar-upstream`, `svc-azure-registry-pull`).
  - Runtime: tokens are group-scoped with no username. Record what the request
    log shows for them before relying on it for audit.
- **Uncached pulls only prove auth.** A cached image proves nothing: on AKS
  that was a node that already had the image, and here it is AR's cache or a
  warm replica. Verify with a tag that nothing has pulled yet. The console's
  Verify step already treats a cached pull as inconclusive.
- **Check every layer, not the summary.** On AKS, the values, the rendered
  release and the node file disagreed. Here the layers are:
  - Terraform outputs.
  - The platform's live config (`az containerapp show`, `gcloud run services describe`).
  - The rotator's JSON log lines.
  - `python -m jfrog_oidc claims` / `check` from inside the workload.
