# JFrog OIDC for serverless — project notes for Claude Code

Short-lived JFrog tokens for Cloud Run (services, jobs, functions), Azure
Container Apps and Azure Functions (Flex, Premium/Dedicated, Functions on ACA),
using OIDC token exchange. Nothing long-lived is stored anywhere.

We are now building **the console**: a local web UI that configures, simulates
and verifies this for a target. The full spec is in `docs/console-spec.md`; read
it before starting any milestone. Record progress in `docs/progress.md`.

## Layout

- `client/` — `jfrog_oidc`, a stdlib-only Python client: identity sources, the
  exchange with caching, helpers and a CLI.
- `rotator/` — the pull-path rotator for GCP (Secret Manager + AR repoint) and
  Azure (Key Vault + ARM refresh).
- `terraform/modules/` — `jfrog-oidc`, `gcp-pull-path`,
  `azure-identity-and-pull-path`. `terraform/examples/complete` wires all
  targets.
- `samples/` — a Cloud Run function and an Azure Function using the client.
- `console/` — to be built, per the spec.
- `kcp_console/` — the KCP console: the AKS kubelet credential provider sibling
  (spec `docs/kcp-console-spec.md`, progress `docs/kcp-progress.md`). Same rules
  as `console/`. It never runs state-changing kubectl/helm; changes go through
  ADO pipeline runs behind approvals.

## Commands

```bash
# client tests
cd client && PYTHONPATH=src python -m unittest discover -s tests -v
# rotator tests
cd rotator && PYTHONPATH=../client/src:. python -m unittest test_rotate -v
# console tests (once console/ exists)
PYTHONPATH=client/src:. python -m unittest discover -s console/tests -v
# simulation scenarios vs expectations (M1+)
PYTHONPATH=client/src:rotator:. python -m console.sim check-all
# terraform (needs network to the provider registry)
terraform -chdir=terraform/examples/complete init -backend=false && terraform -chdir=terraform/examples/complete validate
terraform fmt -recursive -check terraform/
# run the console (M2+)
PYTHONPATH=client/src:rotator:. python -m console
# KCP console: tests, simulator, run (replay fixtures)
python3 -m unittest discover -s kcp_console/tests -t .
python3 -m kcp_console.sim check-all
python3 -m kcp_console --fixtures kcp_console/tests/fixtures/sandbox
```

## Rules

- **Stdlib only** in `client/`, `rotator/` and `console/`. No pip dependencies
  and no JS build step. The UI is vanilla ES modules with no CDN.
- **Never print, log, store or return a token.** That covers JFrog access tokens,
  identity JWTs, `JFROG_ACCESS_TOKEN`, Key Vault values and function keys. Route
  every log and SSE line through `console/redact.py`. If you add a log path, add
  it to the redaction test.
- **Simulation runs the real code.** Do not mock `jfrog_oidc` or `rotate.py`
  inside the simulator; emulate the HTTP endpoints they call. If the simulator
  needs a hook, add a seam to the real code (as `clock=`/`sleep=` already are)
  and keep the default behaviour identical.
- **No cloud or JFrog changes without approval.** `terraform apply`, `gcloud …
  update/deploy/execute`, `az … update/set/start` and JFrog writes happen only
  behind the console's approval gates. During development, do not run them
  yourself against real accounts unless I ask in the session.
- **Terraform is the source of truth** for identity, the JFrog side and the pull
  path. The console generates JSON-syntax workspaces (`main.tf.json`) that call
  the modules. Never string-template HCL.
- **Subprocesses:** argv lists only, never `shell=True`, always a timeout, and
  always through `console/shell.py`.
- **Server:** bind to 127.0.0.1, require the session token, check Host/Origin,
  set a strict CSP.
- **Tests first** for every step module. Use recorded fixtures for gcloud/az
  JSON and a fake `terraform` script in `console/tests/fixtures/`. Unit tests
  never need cloud access.

## Known unverified items

These come from the README's "Verify in your environment" section. Fix them in
code when real providers or APIs disagree:

- The `jfrog/platform` value `provider_type = "Azure"` is accepted by the
  JFrog API. (The schemas, including `token_spec`, passed `terraform validate`
  against jfrog/platform 2.2.11 in M4; only the API-side values remain.)
- The Functions-on-ACA `Microsoft.App` API version that accepts
  `kind = functionapp`.
- The Container Apps log table name used in the Azure alert.
- The wire step's az flags: `az containerapp secret set --secrets
  name=keyvaultref:<url>,identityref:<id>` and `az containerapp registry set
  --password-secret-name`.
- The Docker subdomain host form `<server>-<repo-key>.jfrog.io` on the target
  JFrog instance (it is what the AKS pilot's instance served; a self-hosted
  platform may differ: set `jfrog.docker_host`).

Verified in M4 by `terraform validate` (azurerm 5.12.0): the
`azurerm_function_app_flex_consumption` argument names. Verified by `terraform validate`
(Terraform 1.15.5, jfrog/platform 2.2.11, google 8.9.0, azurerm 5.12.0) on both
examples: the `jfrog-oidc` module validations from the AKS pilot lessons. Verified live: the AR
PATCH `updateMask` (gcloud sends the same mask).

Known blocker (2026-10-30, JFrog Cloud trial): Artifact Registry remote repos
get 403 from the JFrog Cloud edge for any credentials (even anonymous); the
requests never reach Artifactory's request log. GCP image pulls through AR need
JFrog to allow that traffic. The runtime path (in-process exchange) works, and so
does bringing images in at build time (OIDC token, pull from JFrog by repository
path, push to AR), which avoids the AR remote. Proven with Cloud Build; CI/CD is
Azure DevOps (for both Azure and GCP targets), so build and deploy pipelines are
ADO only. Cloud Build and GitHub Actions are out of scope for now.
