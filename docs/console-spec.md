# JFrog OIDC Console — build spec

A local web console. You enter a JFrog endpoint and a target (a Cloud Run service, job or function; a Container App; or an Azure Function on any hosting model). The console then sets up short-lived JFrog tokens for that target, tests them end to end, and reports when the target is ready. It also has a simulation mode that runs the real client and rotator code against emulated JFrog, GCP and Azure. The simulation uses a fast-forward clock and fault injection.

Decisions already made:
- **Terraform applies the changes.** The console generates a workspace from `terraform/modules`, shows the plan, and applies it only after approval.
- **The console runs locally.** It uses the operator's `gcloud`/ADC and `az` logins and reads `JFROG_ACCESS_TOKEN` from the environment. It binds to 127.0.0.1 only.
- **The simulation runs real code.** `client/jfrog_oidc` and `rotator/rotate.py` run unmodified, apart from clock injection, against in-process emulators.

## 1. Scope

In scope:
- Target kinds:
  - `gcp_run_service`
  - `gcp_run_job`
  - `gcp_run_function`
  - `azure_container_app`
  - `azure_functions_aca` (Functions on Container Apps)
  - `azure_functions_premium` (Premium or Dedicated, custom container)
  - `azure_functions_flex` (Flex Consumption, code)
  - `aws_lambda`, `aws_ecs_task`, `aws_app_runner` (runtime path only, §11)
  - `openshift_knative_service` (OpenShift Serverless or any Knative cluster; runtime path only, §12)
- For each target:
  1. Discover its identity.
  2. Create the JFrog integration and identity mapping.
  3. Create or extend the pull path where the target pulls from JFrog.
  4. Wire the runtime env and registry settings onto the target.
  5. Run the first rotation.
  6. Verify live.
  7. Produce a "ready" report.
- Simulate any saved config, or one of the built-in scenarios.

Out of scope for v1:
- Multi-user hosting and RBAC.
- A JFrog image-pull path on AWS and Knative (§11.5, §12.4).
- Managing the target's own deployment (image builds, revisions). The console owns identity, the JFrog side and the pull path. It only *patches* env and registry settings on the target, and only after showing a diff.

## 2. Repository layout to add

```
console/
  __main__.py          python -m console [--port 8765] [--no-browser]
  server.py            ThreadingHTTPServer, routing, auth, SSE
  model.py             dataclasses + JSON (de)serialisation, validation
  store.py             .console/ persistence (configs, runs, workspaces) — never secrets
  runner.py            pipeline orchestration, step state machine, event bus
  steps/
    preflight.py       tools, logins, JFrog token, permissions
    discover.py        gcloud/az lookups → identity facts
    tfgen.py           workspace rendering (main.tf.json + terraform.tfvars.json)
    terraform.py       init/plan/show -json/apply, plan summary, guards
    wire.py            env/registry patch on the target (diff → approve → apply)
    rotate_first.py    trigger + await first rotation
    verify.py          live probes, negative tests, evidence
    report.py          Markdown/JSON ready report
  shell.py             subprocess wrapper: argv only, timeouts, redaction, recorded-fixture mode
  redact.py            token/JWT/secret scrubbing for logs and SSE
  sim/
    clock.py           VirtualClock (now, advance_to, sleep)
    engine.py          discrete-event loop, scenario execution, metrics
    scenario.py        scenario schema + loader + built-ins
    emulators/
      server.py        one in-process HTTP server, path-prefixed per emulator
      jfrog.py         OIDC integrations, mappings, token minting, Artifactory reads, registry auth
      gcp.py           metadata identity/token, Secret Manager, Artifact Registry remote repos
      azure.py         managed identity endpoint, Entra token versions, app-role gate, Key Vault, ARM refresh
      platforms.py     pull behaviour: AR cache, ACA secret refresh lag, App Service KV-ref cache
    __main__.py        python -m console.sim run <scenario|config> [--json]
    scenarios/*.json   built-in scenarios (section 7.4)
  static/
    index.html, app.js, styles.css   no build step, no CDN
  tests/
    fixtures/          recorded gcloud/az JSON outputs, fake terraform binary
    test_*.py
```

Stack: Python 3.11+, standard library only (matching the client). The UI is vanilla JS with ES modules. Charts are hand-built SVG. The UI makes no external network calls.

## 3. Data model (model.py)

```jsonc
// .console/configs/<name>.json — safe to commit; holds no secrets
{
  "name": "orders-prod",
  "jfrog": {
    "url": "https://acme.jfrog.io",          // origin only: no /artifactory, no trailing path
    "docker_repo": "docker-virtual",         // images pull from acme-docker-virtual.jfrog.io
    "docker_host": "",                       // override for that host (self-hosted naming)
    "reader_group": "serverless-readers",
    "project_key": null,
    "integration_names": { "gcp": "gcp-cloudrun", "azure": "azure-entra" }
  },
  "target": {
    "kind": "azure_container_app",           // see §1
    "gcp":   { "project": "", "region": "", "name": "" },
    "azure": { "subscription": "", "resource_group": "", "name": "",
               "environment_id": "", "key_vault_id": "" }
  },
  "runtime": { "enabled": true, "ttl_seconds": 3600, "username": "svc-azure-aca-orders" },
  "pull_path": {
    "enabled": true,
    "ttl_seconds": 14400, "rotation_cron": "23 * * * *",
    "gcp_repositories": { "jfrog-docker": { "format": "DOCKER", "upstream_uri": "" } }, // "" = https://<jfrog.docker_host>
    "registry_username": "svc-azure-registry-pull"
  },
  "verify": {
    "artifact_path": "generic-local/healthcheck/ping.txt",   // runtime read test
    "probe_image_runtime": "us-docker.pkg.dev/<p>/platform/jfrog-oidc-probe:0.1.0", // NOT via JFrog
    "probe_image_pull":    "jfrog-oidc-probe:0.1.0"   // via JFrog, relative to the Docker repo host / AR remote
  },
  "state_backend": { "type": "local" }       // or {"type":"gcs",...} / {"type":"azurerm",...}
}
```

Validation is shared between the UI (via `GET /api/schema`) and the server. These rules emit warnings, which the UI shows inline:
- `pull_path.ttl_seconds < 2 * rotation_interval + platform_pickup_lag` gives a warning. Pickup lag is 0 for AR, 30 min for Container Apps, and 24 h for Premium Functions when ARM refresh is not permitted.
- Runtime TTL under 15 min, or over 12 h.
- An Azure target with only a system-assigned identity. Live probes cannot borrow it; offer the self-test endpoint (§6.3) instead.

## 4. Pipeline (runner.py)

Steps run in order. Each step is idempotent, records evidence, and can be re-run on its own.

| # | Step | Reads/changes | Gate |
|---|---|---|---|
| 1 | Preflight | read-only | — |
| 2 | Discover | read-only | — |
| 3 | Simulate (this config) | none | Warn on any failing period |
| 4 | Plan | `terraform plan` | — |
| 5 | Apply | `terraform apply <planfile>` | **Explicit approval**. Typed confirmation if the plan has any delete or replace |
| 6 | Wire target | gcloud/az update | **Approval of the shown diff** |
| 7 | First rotation | job execute | — |
| 8 | Verify | probes | — |
| 9 | Report | file | — |

State machine per step: `pending → running → (succeeded | failed | needs_approval | skipped)`. Events go to an in-memory bus and are persisted to `.console/runs/<run-id>/events.jsonl`. The UI subscribes over SSE.

### 4.1 Preflight

Required tools: `terraform` ≥ 1.6, and `gcloud` and/or `az` depending on the target. Checks, each reporting `ok`, `warn` or `fail` with a fix hint:
- `gcloud auth print-access-token` (ADC) and the active project.
- `az account show` and the subscription match.
- JFrog: `GET {url}/access/api/v1/system/ping`.
- JFrog token: authenticate and list OIDC integrations, `GET /access/api/v1/oidc` (needs admin or project admin).
- The reader group exists: `GET /access/api/v2/groups/{name}`.
- Permissions. GCP: `gcloud projects get-iam-policy` is too broad, so call `testIamPermissions` on the project with the permissions the modules need. Azure: `az role assignment list --assignee` for the current principal at the RG/vault scope. Report gaps and do not block.

### 4.2 Discover

| Kind | Command | Facts |
|---|---|---|
| gcp_run_service | `gcloud run services describe NAME --region R --format=json` | `spec.template.spec.serviceAccountName`, image |
| gcp_run_job | `gcloud run jobs describe ...` | `spec.template.spec.template.spec.serviceAccountName` |
| gcp_run_function | `gcloud functions describe NAME --region R --gen2 --format=json` | `serviceConfig.serviceAccountEmail`, `buildConfig.serviceAccount` |
| all GCP | `gcloud iam service-accounts describe EMAIL --format=json` | `uniqueId` (mapping subject) |
| azure_container_app / functions_aca | `az containerapp show -g RG -n NAME` | `identity.userAssignedIdentities{principalId,clientId}`, `kind`, `properties.configuration.registries` |
| azure_functions_premium / flex | `az functionapp show -g RG -n NAME` + `az functionapp plan show` | identity, `keyVaultReferenceIdentity`, plan SKU (EP*/P*/FC1) |

Discovery can correct the user's chosen kind. For example, a Function App on an FC1 plan becomes `azure_functions_flex`; the UI shows the correction.

GCP default compute service accounts make the mapping overly broad, because every workload in the project would share one identity. In that case, warn and offer to create a dedicated SA. The console doesn't swap the target's SA automatically.

### 4.3 Terraform workspace (tfgen.py, terraform.py)

- Workspace: `.console/workspaces/<config-name>/` containing `main.tf.json`, `terraform.tfvars.json`, `backend.tf.json`, and `.terraform.lock.hcl` after init.
- Write JSON syntax only, never string-templated HCL. Module `source` is the absolute path to `terraform/modules/<module>`.
- Module instances per target:
  - `jfrog-oidc`: integrations for the needed cloud(s); mappings `<target>-runtime` and, if the pull path is on, `<target>-pull`.
  - `gcp-pull-path` (GCP with the pull path on, or any `gcp_run_function` using remote package repos). One instance per project+region is **shared** across targets; key it in the workspace by project/region and merge repositories.
  - `azure-identity-and-pull-path`: one instance per subscription and tenant, shared. Add the target's identity to `audience_assignees` and `secret_readers`, and Premium apps to `function_app_ids`.
- Several configs that share a project or subscription must share one workspace. A per-config workspace would create duplicate audience apps and rotators. Rule: workspace key = `gcp:<project>:<region>` or `azure:<subscription>`. A config adds its own entries to the shared workspace's tfvars maps, under a file lock.
- `terraform plan -out=plan.bin -input=false -lock-timeout=60s`, then `terraform show -json plan.bin`. Summarise the `resource_changes` actions per resource, and any `delete`/`replace`.
- Apply only `plan.bin`, and only if the plan file's hash matches the plan the operator approved.
- Never pass secrets as variables. Bootstrap values stay placeholders.

### 4.4 Wire target (wire.py)

Compute the desired settings, read the current ones, show a diff, and apply on approval. Use argv-only subprocess calls.

| Kind | Runtime env | Pull path |
|---|---|---|
| gcp_run_service / job | `gcloud run services|jobs update --update-env-vars JFROG_URL=…,JFROG_OIDC_PROVIDER=…,JFROG_OIDC_AUDIENCE=…,JFROG_OIDC_SOURCE=gcp` | Show the recommended image path through AR (`<region>-docker.pkg.dev/<p>/jfrog-docker/<image>`). Don't redeploy the image automatically |
| gcp_run_function | `gcloud functions deploy NAME --update-env-vars …` is a redeploy, so prefer `gcloud run services update` on the underlying service | Show the `--index-url` line for `requirements.txt` |
| azure_container_app | `az containerapp update --set-env-vars …` (plus AZURE_CLIENT_ID) | `az containerapp secret set --secrets jfrog-registry-token=keyvaultref:<url>,identityref:<mi>`, then `az containerapp registry set --server <host> --username <u> --password-secret-name jfrog-registry-token` |
| azure_functions_aca | as container app | as container app |
| azure_functions_premium | `az functionapp config appsettings set` | `az functionapp config container set` with the registry password set to `@Microsoft.KeyVault(SecretUri=…)`, plus `keyVaultReferenceIdentity` |
| azure_functions_flex | `az functionapp config appsettings set` | none |

Also emit IaC snippets for the operator's own repo, Terraform/Bicep for the same settings, so GitOps-managed targets don't drift back.

### 4.5 First rotation

- GCP: `gcloud run jobs execute <rotator> --wait`, then read `rotation_succeeded` from logs (§6.4).
- Azure: `az containerapp job start`, then poll `az containerapp job execution show` until it finishes.

## 5. Live verification (verify.py)

Every check records the command, the result and a redacted excerpt. The ready report lists them all.

### 5.1 Runtime probe — "this identity can mint JFrog tokens"

Run a short-lived probe **as the target's identity**:
- GCP: create or update the Cloud Run job `jfrog-oidc-probe-<target>` with `--service-account <target SA>`, image `verify.probe_image_runtime`, and the same env as the target. Its command is `python -m jfrog_oidc claims && python -m jfrog_oidc check && python -m jfrog_oidc.probe read <artifact_path>`. Execute it with `--wait` and parse its JSON log lines.
- Azure (user-assigned MI): create or update the Container Apps job `jfrog-oidc-probe-<target>` (manual trigger) in the target's environment, or in the rotator's environment for Functions. Attach the same MI, start it, poll it, and read logs (§6.4).
- Pass when: the claims match the mapping (`sub` or `oid`/`tid`/`aud`), the exchange succeeds, the token's scope matches the mapping's groups, the TTL is within ±60 s of the mapping's `expires_in`, and the artifact read returns 200.

### 5.2 Negative probes — "trust is pinned"

Use the same probe with deliberately wrong inputs. Each must fail with 401/403:
- A wrong audience (`JFROG_OIDC_AUDIENCE=jfrog-wrong`).
- The correct audience but another identity, using the rotator identity against the runtime mapping. It must not receive the runtime mapping's group scope.
- The artifact read must fail on a repo outside the reader group's permission (`verify.forbidden_path`, optional).

### 5.3 Pull probe — "the platform can pull through the pull path"

- GCP: a probe job whose image is `…-docker.pkg.dev/<p>/jfrog-docker/<verify.probe_image_pull>`. Starting it proves AR → JFrog with the rotated token. To prove an uncached pull, use a unique tag the operator publishes once (`probe-<run-id>`), or skip that and mark it "cached pull only".
- Container Apps / Functions on ACA: a probe job with the same `registries` and `secrets` config as the target, pulling `<jfrog-host>/<probe_image_pull>`.
- Premium Functions: `GET …/sites/<app>/config/configreferences/appsettings?api-version=2022-03-01` must show `DOCKER_REGISTRY_SERVER_PASSWORD` with status `Resolved`. Then restart the app with approval and confirm it reaches `Running`.
- Flex: not applicable.

### 5.4 Self-test endpoint (fallback for system-assigned identities)

`client/jfrog_oidc/probe.py` provides `selftest()`, which returns the `claims` and `check` results and an artifact read status. It never returns a token. The samples expose it at `GET /healthz/jfrog`, protected by a function key or by Cloud Run IAM. The console calls the endpoint with the operator's credentials: `gcloud auth print-identity-token` for Cloud Run, or a function key fetched through `az functionapp function keys list`.

### 5.5 Pull-path health

- Read the rotator's last success time from the logs.
- Read the expiry of the current secret: Key Vault `attributes.exp`, or the Secret Manager version create time plus TTL.
- Fail if remaining validity is less than 2 × the rotation interval.

## 6. Server (server.py)

### 6.1 Security

- Bind to `127.0.0.1` only. Refuse to start if `--host` is anything else, unless the operator passes `--i-know-this-exposes-credentials`.
- On start, generate a 32-byte session token and print `http://127.0.0.1:8765/#t=<token>`. The UI stores the token in memory and sends it as `X-Console-Token` on every API call. Compare with `hmac.compare_digest`. Requests without the token get 401. Check `Host` and `Origin` to stop DNS rebinding: reject unless they are `127.0.0.1:<port>`/`localhost:<port>`.
- Responses set a CSP with `default-src 'self'`, no inline script, and `frame-ancestors 'none'`.
- Run every subprocess through `shell.py`: argv only, never `shell=True`, a per-call timeout, an env allowlist, and redaction of stdout and stderr before storage or SSE.
- `redact.py` masks JWT-shaped strings (`eyJ…\.…\.…`), JFrog reference tokens, `Authorization` headers, `keyvaultref` values and `--password*` argv values. A test feeds known tokens through every log path and asserts none survive.
- Nothing secret is written to `.console/`. `JFROG_ACCESS_TOKEN` is read from the environment at call time and passed to Terraform as `JFROG_ACCESS_TOKEN`, never through tfvars.

### 6.2 API

```
GET  /api/schema                          config JSON schema + target kinds + defaults
GET  /api/configs  · PUT /api/configs/{n} · DELETE /api/configs/{n}
POST /api/validate                        → {errors[], warnings[], ttl_math{}}
POST /api/runs        {config, steps?}    → {run_id}
GET  /api/runs/{id}                       → step states + evidence
GET  /api/runs/{id}/events                SSE: step, log, approval_required, evidence, done
POST /api/runs/{id}/approve {step, plan_sha256?, typed_confirmation?}
POST /api/runs/{id}/cancel
GET  /api/runs/{id}/report?format=md|json
POST /api/sim         {scenario | config, overrides?} → {sim_id}
GET  /api/sim/{id}                        → timeline, metrics, verdict, events
GET  /api/sim/scenarios                   built-ins
```

### 6.3 Concurrency

- Each run uses one worker thread.
- Only one Terraform operation may run per workspace, enforced by a file lock. A second run on the same workspace waits, and the UI shows the wait.

### 6.4 Log retrieval

- GCP: `gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="<job>" AND labels."run.googleapis.com/execution_name"="<exec>"' --format=json --freshness=1h`.
- Azure: prefer `az containerapp job logs show` if the installed CLI has it. Fall back to a Log Analytics query through `az monitor log-analytics query`, with the workspace taken from the environment's `appLogsConfiguration`.
- Both: parse only lines that are valid JSON with an `event` field.

## 7. Simulation (console/sim)

### 7.1 Principle

The simulator executes the **real** `jfrog_oidc` client and `rotator/rotate.py`. The emulators are HTTP endpoints, not mocks of the client. The client and rotator reach them through the environment overrides they already support:
- `GCE_METADATA_HOST`
- `IDENTITY_ENDPOINT` / `IDENTITY_HEADER`
- `SECRET_MANAGER_API`
- `ARTIFACT_REGISTRY_API`
- `ARM_API`
- `KEY_VAULT_URI`
- `JFROG_URL` with `JFROG_OIDC_ALLOW_HTTP`

Time is virtual. Emulators and code share a `VirtualClock`:
- The client already accepts `clock=` and `sleep=`.
- **M0 refactor:** `rotate.py` must take `clock` and `sleep` too. Thread them through `main(clock=time.time, sleep=time.sleep)`, `check_ttl` and `http()` retries. The CLI behaviour stays unchanged.

### 7.2 Emulator fidelity

These behaviours must match the real services, because they decide outcomes.

**JFrog** (`jfrog.py`):
- Integrations have a `name`, `issuer` and `audience`.
- Mappings have `claims`, `priority` and `token_spec`. Matching works the way JFrog documents it: claims in the mapping must equal the token's claims; the lowest priority number among the matches wins; no match returns 401.
- Subject tokens are JWTs signed with HS256 by the emulated issuer. Validate signature, `iss`, `exp` and `nbf`.
- Issued access tokens are JWTs with `sub=jfac@sim/users/<username>` and `exp = now + expires_in`.
- Expose `/artifactory/api/storage/...`, `/artifactory/<repo>/<path>`, and a registry auth check that accepts Basic `<username>:<token>` only while the token is unexpired and the username matches.
- Faults: outage windows (connection refused or 503), per-integration misconfiguration, and revoked mappings.

**GCP** (`gcp.py`):
- The metadata `identity` endpoint returns a token for the configured SA. Include `email` only when `format=full`. Respect `audience`.
- The metadata `token` endpoint returns an access token.
- Secret Manager versions, with `enabled`/`disabled` state and `:addVersion`, `:disable` and list with filter.
- Artifact Registry remote repos store a `usernamePasswordCredentials` pointer. PATCH honours `updateMask`. An uncached pull resolves the pointer's version; if that version is disabled or its token has expired, the pull fails. Cached artifacts are served without contacting JFrog.

**Azure** (`azure.py`):
- The MI endpoint requires `X-IDENTITY-HEADER` and honours `client_id`.
- The token version follows the audience app's `requestedAccessTokenVersion`. v1 tokens use `iss=https://sts.windows.net/<tid>/` and `aud=api://<app>`; v2 tokens use the v2 issuer and `aud=<app-id>`.
- If the app-role gate is on and the identity is unassigned, return `AADSTS501051`-style 400.
- Key Vault secrets are versioned and carry `exp`.
- ARM `configreferences/appsettings/refresh` returns 200, or 403 when the rotator lacks permission.

**Platforms** (`platforms.py`):
- Cloud Run cold start pulls through AR.
- A Container Apps replica start reads its cached registry secret. The cache refreshes at a random time 5–30 min after a new Key Vault version.
- Premium Functions cache KV references for 24 h unless refreshed through ARM.
- Flex never pulls.
- Runtime requests call the client's `token()`, then an artifact read.

### 7.3 Engine

- A discrete-event loop with a priority queue of `(time, seq, event)`.
- Event sources:
  - Rotator cron ticks.
  - Platform cold starts and scale-outs: Poisson, with a configurable rate.
  - Runtime request bursts.
  - Fault windows opening and closing.
  - Alert evaluation every 15 min, using the same rule as the modules: no success within `alert_after`.
- The engine runs the real code synchronously at each event, against emulators on a background thread. A 24 h scenario should finish in under 10 s.
- Metrics, as time series at 1 min resolution:
  - Each JFrog token's validity interval.
  - The credential version each platform holds.
  - Pull attempts, ok or fail with a reason.
  - Runtime requests, ok or fail with a reason, including "served cached token during outage".
  - Alert state.
- Verdict:
  - `pass` when there are no failed pulls or requests.
  - `degraded` when failures happened only inside injected outage windows, and only on uncached pulls.
  - `fail` otherwise. A failure verdict includes the first failure's causal chain, built from events. For example: "rotations 03:23, 04:23, 05:23 failed (fault rotator_down) → token v7 expired 06:23 → ACA replica start 06:31 failed: registry 401".

### 7.4 Scenarios (scenarios/*.json)

Each built-in scenario has an asserted expected verdict, enforced in tests.

| Scenario | Faults | Expected |
|---|---|---|
| happy-gcp-function | none, 24 h | pass |
| happy-aca | none, 24 h | pass |
| happy-functions-premium | none, 24 h | pass |
| jfrog-outage-90m | JFrog 503 02:00–03:30 | degraded: uncached pulls fail inside the window; runtime serves cached tokens until expiry |
| rotator-misses-2-runs | rotator runs fail 03:00–05:00 (ACA) | pass: the 02:23 token is valid until 06:23, and the 05:23 run succeeds with time left for the ACA refresh lag |
| rotator-misses-3-runs | rotator runs fail 03:00–06:00 (ACA) | fail: the new token arrives at 06:23, exactly when the old one expires, so replica starts during the ACA lag fail. Alert fires ≈05:23, an hour before the first failure. This is why the default alert is 3 h |
| rotator-down-5h | rotator runs fail 03:00–08:00 | fail: pulls fail from 06:23; alert fired ≈05:23 |
| ttl-too-short | pull mapping TTL 3600, hourly rotation, ACA | fail. `MIN_TTL_SECONDS` defaults to 1800, so rotations succeed, but each token expires at the next rotation and ACA takes 5–30 min to pick up the new one: replica starts in that gap fail. Root cause names the pull TTL vs interval + pickup lag |
| min-ttl-refusal | pull mapping TTL 1200 (below `MIN_TTL_SECONDS` 1800), ACA | fail. Every rotation is refused by `MIN_TTL_SECONDS`, so the platform keeps the bootstrap placeholder |
| premium-no-refresh-permission | ARM refresh 403, TTL 4 h | fail: KV ref cache serves an expired token; rotator runs are marked failed |
| azure-v1-tokens | audience app v1 | fail at the first exchange: issuer mismatch; root cause names `requestedAccessTokenVersion` |
| wrong-audience | runtime env audience typo | fail: 401 on every runtime request; pull path unaffected |
| sa-recreated | at 05:00 the SA is recreated (new `uniqueId`) | runtime fails from 05:00 with "no mapping matched sub" |

Scenario schema: `{name, target_kind, duration, clock_start, config_overrides{}, load{cold_starts_per_hour, requests_per_minute}, faults:[{type, start, end, params}], expect:{verdict, max_failed_pulls?, alert_fired_before?}}`. Loading a saved console config as a scenario uses its real TTLs, cron and target kind, with no faults.

### 7.5 Headless

```
python -m console.sim run rotator-down-3h            # human summary
python -m console.sim run .console/configs/orders-prod.json --json
python -m console.sim check-all                       # CI: all built-ins vs expectations
```

## 8. UI (static/)

The layout has three tabs: **Configure**, **Simulate** and **Runs**.

**Configure**
- JFrog card:
  - URL.
  - Token status, read from the server: "JFROG_ACCESS_TOKEN present, admin ✓".
  - Reader group, typeahead from `/access/api/v2/groups`.
  - Optional project key.
- Target card: a kind selector with icons for the 7 kinds; fields per kind; a **Discover** button that fills identity facts and corrects the kind.
- Pull path card: an on/off toggle; repo rows for GCP; Key Vault and registry username for Azure.
- TTL card: runtime TTL, pull TTL and rotation cron. Show a live "survives N missed rotations" figure and pickup-lag warnings from `/api/validate`.
- Buttons: Save · Simulate this config · Run pipeline.

**Run view**
- A vertical stepper with the 9 steps.
- The selected step shows its evidence:
  - The plan summary table: creates, updates, and destroys in red.
  - The wire diff, side by side.
  - The probe results checklist, each with ✓/✗, evidence and a fix hint.
- Approval buttons appear inline. Destroys and replaces need typed confirmation.
- A live log pane on the right, fed by SSE, redacted.

**Simulate**
- A scenario picker, or "current config".
- Fault toggles with time pickers.
- A load slider.
- Run.
- Results:
  - A swim-lane timeline (SVG) with lanes for rotator runs, token validity bars (labelled by version), the credential each platform holds, pull attempts as dots (green/red), runtime requests as a density strip, alert state and fault windows (hatched).
  - A verdict panel with the causal chain.
  - An event table, filterable.
- Use the same timeline component for live runs. It shows the real rotator history from logs.

**Ready report**
- A Markdown export covering:
  - The target and identity facts.
  - The JFrog integration and mapping, with the claims JSON.
  - The resources created, from plan JSON.
  - The settings wired.
  - Every verification with evidence.
  - Rollback steps:
    - `terraform destroy -target` for the mapping.
    - The wire diff, reversed.

Accessibility and quality:
- Keyboard navigable.
- Works at 1280 px and down to 1024 px.
- Light and dark themes via `prefers-color-scheme`.
- No external fonts or scripts.

## 9. Milestones

Build in order. Each milestone ends with its tests green and a short demo note in `docs/progress.md`.

**M0 — Prep**
- Thread clock and sleep through `rotate.py`.
- Add `client/src/jfrog_oidc/probe.py` (`selftest()`, plus CLI `python -m jfrog_oidc.probe read <path>`).
- Add `/healthz/jfrog` to both samples.
- Acceptance: the existing client and rotator tests pass, with new tests for the probe and for clock injection.

**M1 — Simulation engine (headless)**
- Emulators, engine, all scenarios in §7.4, `python -m console.sim check-all`.
- Acceptance: every scenario meets its expectation, and a 24 h scenario runs in under 10 s.
- Mutation check: lowering a mapping TTL in a happy scenario must flip it to fail.

**M2 — Server + Simulate tab**
- `server.py` with auth and CSP, `/api/sim*` and `/api/validate`, and the Simulate tab with the timeline.
- Acceptance tests:
  - No token → 401.
  - A wrong Host → 403.
  - The SSE stream works.
- The UI renders every built-in scenario. Do a manual check in a browser, and record a screenshot in `docs/`.

**M3 — Preflight + Discover**
- Read-only gcloud/az/JFrog calls through `shell.py`, with a recorded-fixture mode.
- Acceptance: fixture tests for all 7 kinds, including the kind correction for FC1 and the default-SA warning.
- Run it once against a real non-prod project and subscription.

**M4 — Plan/Apply + Wire**
- `tfgen`, the shared-workspace key and lock, `terraform.py` with plan-hash-gated apply, and `wire.py` with diff and approval.
- Acceptance:
  - `terraform validate` passes for generated workspaces for every kind.
  - A fake `terraform` binary in tests covers the approval gates.
  - Apply is refused without approval, refused on a plan-hash mismatch, and refused on a delete without typed confirmation.
- Before M4 is accepted, run `terraform validate` on `terraform/stacks/all-targets` and fix whatever the real providers reject. That code has never seen real providers.

**M5 — First rotation + Verify + Report**
- Implement §5 in full.
- Acceptance:
  - Against non-prod, one GCP function and one Container App reach "Ready" with all positive and negative probes green.
  - Breaking the mapping on purpose (wrong `sub`) turns the runtime probe red with a correct fix hint.

**M6 — Hardening**
- A redaction test across all log paths, a CSP check, a bind check, and README docs.
- Make sure `python -m console` works from a fresh clone with only Python and the cloud CLIs installed.

## 10. Definition of done

- One command starts it: `python -m console`.
- For a new Cloud Run function and a new Container App in non-prod, the operator:
  1. fills in the form,
  2. presses Discover,
  3. presses Simulate and sees `pass`,
  4. presses Run, then approves the plan and the wire diff,
  5. gets a Ready report with all probes green,
  6. receives no JFrog secret anywhere: not in UI logs, `.console/`, Terraform state, or app settings beyond the Key Vault reference.
- `python -m console.sim check-all` and all unit tests run in CI without cloud access.

## 11. AWS targets (Lambda, ECS, App Runner)

Sidebar item **AWS** (`/aws`). Code: `console/steps/aws.py` (mixins of Preflight, Discover and Wire), `client/src/jfrog_oidc/sigv4.py` and `AwsStsSource`, `terraform/modules/aws-identity`, workspace `terraform/workspaces/aws_<account>`.

### 11.1 Identity and token

- The identity is the workload's IAM role: the Lambda execution role, the ECS task role, the App Runner instance role. Never a shared one: a role used by more than one workload, an ECS task role equal to its execution role, or a well-known shared name gets a dedicated role `jfrog-<name>` instead (§3 `dedicated_identity`). The role it replaces keeps its policies; Discover lists them so the operator can attach what the app still needs.
- The JWT comes from STS `GetWebIdentityToken` (IAM outbound identity federation): a SigV4-signed `POST /` to the **regional** endpoint (not the global one), `Audience.member.1 = jfrog-aws`, `SigningAlgorithm = RS256`, `DurationSeconds` 60–3600 (client default 900). XML response; `iss` is the account's issuer `https://<id>.tokens.sts.global.api.aws`, `sub` the role ARN.
- Credentials for signing: Lambda's `AWS_ACCESS_KEY_ID`/`…SECRET…`/`AWS_SESSION_TOKEN`; ECS and App Runner's container credentials endpoint (`…_RELATIVE_URI` on 169.254.170.2, or `…_FULL_URI` with `AWS_CONTAINER_AUTHORIZATION_TOKEN[_FILE]`; `FULL_URI` must be a local host). Region: `JFROG_OIDC_AWS_REGION` (set by Wire, since Lambda reserves `AWS_REGION`), then `AWS_REGION`, `AWS_DEFAULT_REGION`.
- The role needs `sts:GetWebIdentityToken`, limited by the module to the JFrog audience (`sts:IdentityTokenAudience`) and the TTL (`sts:DurationSeconds`).

### 11.2 Preflight

`aws --version`; `sts get-caller-identity` (account must match the target; root is a warning and skips the policy simulation); account aliases (prod warning); `iam get-outbound-web-identity-federation-info` — **fail** when federation is off (`FeatureDisabled`), fix: `aws iam enable-outbound-web-identity-federation --region <region>` (once per account); the issuer's discovery document must be reachable; `iam simulate-principal-policy` for the caller's rights per kind.

### 11.3 Discover

Reads the issuer first (fails with the enable command when off), then per kind: `lambda get-function-configuration` (+ `list-functions` to count users of the role), `ecs describe-services` (when an ECS cluster is set) and `describe-task-definition`, `apprunner list-services`/`describe-service`. Then `iam get-role` for the mapping subject (the role ARN). Sets `jfrog.integration_names.aws = aws-<account>`. Calls that return environments run as sensitive: values never reach the plan, logs or the store.

### 11.4 Workspace and Wire

- `aws_<account>`: provider `aws` (`allowed_account_ids`), module `aws-identity` (dedicated roles with per-kind trust and `aws:SourceAccount`, the inline `jfrog-oidc-web-identity` policy on every role, optional `aws_iam_outbound_web_identity_federation`), module `jfrog-oidc` with the `aws` integration (generic provider, claims `{sub, aud}`). State: S3 with `use_lockfile`.
- Wire sets `JFROG_URL`, `JFROG_OIDC_PROVIDER`, `JFROG_OIDC_AUDIENCE`, `JFROG_OIDC_SOURCE=aws`, `JFROG_OIDC_AWS_REGION`, and switches the role when it is dedicated:
  - Lambda: `update-function-configuration --environment … [--role …]`, then `lambda wait function-updated`.
  - ECS: `register-task-definition --cli-input-json …` (a new revision; every container keeps its other settings), then `update-service --task-definition
    <family>` when a cluster is set.
  - App Runner: `update-service --source-configuration … [--instance-configuration …]`.
- These APIs replace the whole environment, so the plan carries `@resolve:<what>` placeholders and `apply()` fills them from a fresh read. The diff, the plan hash and the logs only ever see the placeholder form.

### 11.5 Pull path (seam)

ECR pull-through cache accepts only fixed upstreams (no custom registry), and Lambda cannot use pull-through at all, so JFrog cannot be an ECR upstream. `model.validate` refuses `pull_path.enabled` for AWS (`no_pull_path`). Images come in at build time: `aws-build-deploy` pulls the base through JFrog with the OIDC token and pushes to ECR. A future seam is ECS `repositoryCredentials` (Secrets Manager) refreshed by a rotator; not built.

### 11.6 Simulation

`console/sim/emulators/aws.py`: the container credentials endpoint and STS `GetWebIdentityToken`, which recomputes the SigV4 signature with the client's own signer, checks the session token, the form, the region and the federation switch, and answers in XML. Fault `federation_disabled` (AWS only); root cause names the enable command. Scenarios: `happy-aws-lambda`, `happy-aws-ecs`, `happy-aws-app-runner`, `aws-federation-disabled`, `aws-role-recreated`.

### 11.7 Pipelines

`aws-workload-onboard` (workspaces `aws_*`, as `ado-setup`) and `aws-build-deploy` (`lambda|ecs|apprunner`, as `ado-build`). Both get their role with AssumeRoleWithWebIdentity on the ADO connection's OIDC token (`AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE`, `oidc-login.sh`). Trust: `terraform/stacks/ado-trust/aws.tf` (IAM OIDC provider for the Entra issuer, roles bound to each connection's `sub`, S3 state bucket, ECR repository).

## 12. Knative targets (OpenShift Serverless or any Knative cluster)

Sidebar item **OpenShift** (`/openshift`). Code: `console/steps/openshift.py`. Everything goes through `kubectl` and a kube context, so any cluster with Knative Serving works; nothing assumes `oc`. Simulation only so far.

### 12.1 Identity

The Knative service's service account, through a projected token with the JFrog audience (`jfrog-k8s`, 3600 s) mounted at `/var/run/secrets/jfrog/token`; the client reads it with the file source. Mapping subject `system:serviceaccount:<ns>:<sa>`. A service on `default` (or a SA several services share) gets a dedicated `jfrog-<name>`. The cluster's issuer (`/.well-known/openid-configuration`) must be https, public, and serve its JWKS without auth: JFrog fetches it. Cluster-internal or private issuers fail with the `serviceAccountIssuer` fix.

### 12.2 Preflight and Discover

Preflight: `kubectl version --client`, the context exists, the Knative CRD `services.serving.knative.dev`, the issuer and its JWKS reachable over http, `auth can-i patch ksvc` / `create serviceaccounts`. Discover: the issuer, then `kubectl get ksvc <name> -n <ns> -o json` (sensitive) and the SA users in the namespace. Sets `jfrog.integration_names.openshift = openshift-<cluster>`.

### 12.3 Workspace and Wire

`openshift_<cluster>`: `kubernetes_service_account_v1` for dedicated SAs, `jfrog-oidc` with the `openshift` integration. State: Kubernetes Secret backend. Wire shows a diff (env, volume, mount, SA) and runs `kubectl patch ksvc … --type merge -p @resolve:ksvc-template`: the template's container list is kept, `metadata.name` dropped so Knative names the revision.

### 12.4 Pull path, simulation, pipeline

No JFrog pull path (`no_pull_path`): the cluster pulls with its own pull secrets. Simulation: `console/sim/emulators/k8s.py` plays the kubelet (writes the token, refreshes at 80% of its lifetime); scenarios `happy-openshift-knative`, `openshift-wrong-audience`. Pipeline `openshift-workload-onboard` logs in with a Kubernetes service connection (`Kubernetes@1`, `KUBECONFIG`); `KUBE_CONTEXT` overrides the console's context name on the agent.
