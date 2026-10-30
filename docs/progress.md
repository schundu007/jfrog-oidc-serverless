# Console build progress

| Milestone | Status | Notes |
|---|---|---|
| Foundation (client, rotator, Terraform modules, samples) | done | 20 client + 5 rotator tests; independent review applied (15 fixes); Terraform syntax/cross-reference checked only, never validated against real providers |
| M0 Prep | done | 2026-10-29 · seams, probe, MIN_TTL 1800 |
| M1 Simulation engine | done | 2026-10-29 · 13 built-in scenarios, all meet expectations |
| M2 Server + Simulate tab | done | 2026-10-29 · screenshot `docs/console-m2-simulate.png` |
| M3 Preflight + Discover | not started | |
| M4 Plan/Apply + Wire | not started | Run `terraform validate` on examples/complete first |
| M5 First rotation + Verify + Report | not started | |
| M6 Hardening | not started | |

Test counts at the end of M2: **client 28, rotator 8, console 83, scenarios 13/13** (`check-all` ≈ 8 s).

---

## M0 Prep

**Built**

- `rotator/rotate.py`: `clock=` / `sleep=` seams on `http`, `check_ttl`, `rotate_gcp`, `rotate_azure` and `main`,
  passed through to `JFrogTokenProvider.from_env`. Defaults are `time.time` / `time.sleep`, so behaviour is unchanged.
  New tests inject a virtual clock and a no-op sleep, and simulate a failed AR PATCH (`patch_fail_codes`).
- `client/src/jfrog_oidc/probe.py`: `read(provider, path)` and `selftest(artifact_path, provider)`. Both return
  identity claims (allow-listed keys only), token username, expiry and the read status. They never return a token.
  CLI: `python -m jfrog_oidc.probe selftest|read`.
- Samples: `/healthz/jfrog` in the Cloud Run function and the Azure Function, using `selftest` (200 / 503).
  `JFROG_HEALTHCHECK_PATH` sets the artifact to read.
- `client/src/jfrog_oidc/sources.py`: the identity fetch reuses one no-proxy opener. Previously it rebuilt the
  opener, and so reloaded the CA bundle, on every call. Same behaviour, much faster in the simulator.
- **MIN_TTL_SECONDS = 1800 everywhere** (decision confirmed in session). The Terraform modules' `min_ttl_seconds`
  default now matches rotate.py. README: the floor is a safety net; size the pull TTL as 2 × interval + pickup lag.
- Spec: `ttl-too-short` rewritten (rotations succeed; each token expires at the next rotation and ACA lag causes
  failures). New `min-ttl-refusal` scenario (TTL 1200 < MIN_TTL 1800 → the rotator refuses and the placeholder stays).

**Demo**: `cd client && PYTHONPATH=src python -m jfrog_oidc.probe --help`

## M1 Simulation engine

**Built**: `console/redact.py`, `console/model.py` (kinds, defaults, cron, pickup lag, TTL math, validation),
`console/sim/` (virtual clock, in-process HTTP emulators for JFrog / GCP metadata + Secret Manager + AR / Azure MI +
Key Vault + ARM, platform models, engine, scenario loader), 13 scenario files, and the `python -m console.sim` CLI.
The real `jfrog_oidc` client and real `rotate.py` run against the emulators. Nothing is mocked inside them.

**Demo**

```bash
PYTHONPATH=client/src:rotator:. python -m console.sim list
PYTHONPATH=client/src:rotator:. python -m console.sim run rotator-misses-3-runs
PYTHONPATH=client/src:rotator:. python -m console.sim run happy-aca --set pull_path.ttl_seconds=1800
PYTHONPATH=client/src:rotator:. python -m console.sim check-all
```

**Modelling choices (documented in the `engine.py` docstring; they are deviations or additions to the spec):**

- **Exposure windows.** A run whose platform holds an expired credential or the placeholder is a `fail`, even if no
  replica start happened to land in the window in that seed. Otherwise `rotator-misses-3-runs` passed by luck.
- `degraded` covers outage-caused request failures, not only uncached pulls during an outage.
- `jfrog-outage-90m` takes down access + registry (`params.services`). Cached tokens keep working: those requests
  count as `ok_cached`.
- Instances live `load.instance_lifetime_minutes` (default 60) and cache their runtime token, so outage tolerance is
  realistic.
- Requests are bucketed per minute. One simulation runs at a time (`SIM_LOCK`), because the seams patch module state.
- ACA pickup lag is sampled from 5–30 min. Premium KV references are cached 24 h unless ARM refresh succeeds.
  Flex never pulls.

## M2 Server + Simulate tab

**Built**

- `console/server.py`, a stdlib `ThreadingHTTPServer`:
  - Binds 127.0.0.1. `python -m console` refuses a non-loopback bind without `--i-know-this-exposes-credentials`.
  - Session token: 32 random bytes, passed in the URL fragment and sent as `X-Console-Token`; compared with
    `hmac.compare_digest`. No cookies.
  - Host allow-list (stops DNS rebinding), plus Origin / `Sec-Fetch-Site` checks.
  - Strict CSP: no `unsafe-inline`, `frame-ancestors 'none'`, `form-action 'none'`.
  - nosniff, no-referrer, DENY framing, COOP/CORP, `Cache-Control: no-store`.
  - JSON-only POST bodies, capped at 1 MiB.
  - Static files are served from an allow-list built at start-up, so path traversal is impossible.
  - API scenarios are restricted to built-in names, so the API can't make it read files from disk.
  - Overrides go through an allow-list. The sim queue is bounded (429 beyond 8 queued).
  - Every JSON response, SSE frame and access-log line goes through `redact`.
- API:
  - `GET /api/health`, `GET /api/schema`, `POST /api/validate`, `GET /api/sim/scenarios`
  - `POST /api/sim` → 202 `{sim_id}`
  - `GET /api/sim/{id}`
  - `GET /api/sim/{id}/events`: SSE with `status`, `event`, `done` / `failed` frames and keep-alives
- `console/static/`, vanilla ES modules with no CDN and no inline script or style:
  - Tabs: Configure and Runs are placeholders for M3–M5.
  - The Simulate tab has a scenario picker, overrides, a fault editor (type, HH:MM window, params JSON), load
    sliders, duration and seed.
  - The run streams progress over fetch-SSE, so the token stays in a header.
  - Results:
    - Verdict panel: stats, causal chain, root cause, TTL math, and whether the verdict matches the expectation.
    - Seven-lane SVG timeline: faults, rotator runs, token validity, platform holds with exposure in red, pulls,
      request density, alert.
    - Filterable event table.
  - Light/dark theme, keyboard tab navigation, works at 1024–1280 px.
  - The token is read from `#t=`, kept only in memory, and removed from the address bar and history, including when
    the URL is pasted into an already-open tab (`hashchange`).

**Acceptance**

- No token → 401, wrong Host → 403, the SSE stream works: covered by `console/tests/test_server.py`.
- The UI renders every built-in scenario. Checked in headless Chromium at 1280 px by driving the real form for all
  13 scenarios:
  - Every verdict matched its expectation, each run took about 1 s, and there were no console errors.
  - Also checked: the wrong-token banner, recovery by pasting the correct URL, and the 1024 px layout.
  - Screenshot (rotator-misses-3-runs): `docs/console-m2-simulate.png`.
- Redaction found a real gap during M2: `?access_token=`, Functions `?code=` keys, SAS `sig=`, `#t=` and
  `x-functions-key`-style headers were not masked. Fixed, and covered by `test_redact.py` and the access-log test.

**Demo**

```bash
PYTHONPATH=client/src:rotator:. python -m console            # opens the browser
PYTHONPATH=client/src:rotator:. python -m console --no-browser
```

## M3 Configure tab: Discover, Preflight, saved configs

**Built**

- `console/shell.py`: the only path to a subprocess or HTTP call.
  - argv lists only, a timeout on every call, stdin closed.
  - The child env is an allow-list; `JFROG_ACCESS_TOKEN` is passed only when a caller hands it over.
  - HTTPS only, redirects not followed.
  - Three modes: `live`, `replay` (recorded fixtures; unmatched calls fail with exit 127 and are reported as
    `fixture_misses`), and `record` (redacted copies; output of sensitive calls is replaced, never stored).
- `console/steps/preflight.py`: read-only checks, each returning ok / warn / fail / skip with a fix hint.
  - terraform ≥ 1.6.
  - gcloud: login, ADC, the project, and `testIamPermissions` per kind.
  - az: account and subscription; production-name heuristic; Owner or Contributor + User Access Administrator /
    RBAC Administrator on the resource group; Key Vault data-plane role.
  - JFrog: ping, OIDC-integration admin rights, an existing integration, the reader group.
- `console/steps/discover.py`: describes the target and fills the mapping subject and identity facts.
  - The mapping subject is the SA `uniqueId` on GCP and the managed identity `principalId` on Azure.
  - **Kind corrections:**
    - An FC1 plan becomes Flex Consumption, and the pull path is turned off.
    - `goog-managed-by=cloudfunctions` means a function.
    - A container app with `kind=functionapp` means Functions on ACA.
    - A name that is found only as the other resource family is corrected to that family.
  - **Warnings:** default compute / App Engine service account; system-assigned identity.
  - **Errors:** gen1 functions, Y1 consumption plans, several user-assigned identities with none chosen, no identity.
- `console/store.py`: saved configs in `.console/configs/<name>.json`.
  - Atomic writes, files 0600 and the directory 0700.
  - A config that redact would change is refused.
- Server: `GET /api/session`; `GET/PUT/DELETE /api/configs[/<name>]`; `POST /api/preflight` and `/api/discover`.
  - One cloud step runs at a time (409 while busy).
  - Every shell line goes through redact into the console log.
- CLI: `--fixtures PATH` (replay) or `--record DIR`. The startup line shows the mode, the az config dir, whether
  `JFROG_ACCESS_TOKEN` is set, and where configs are kept.
- UI: the Configure tab is now the default.
  - Form fields per cloud and kind, with live validation that includes "survives N missed rotations".
  - Discover shows its corrections and facts; Preflight shows a checklist.
  - Save, load and delete (two-step confirm, no browser dialogs), an unsaved-changes guard, and "Simulate this config".
- Redaction: Google `ya29.` access tokens are now masked. This came up while writing the preflight test.

**Acceptance**

- Read-only calls go through `shell.py` with a recorded-fixture mode: covered by `test_shell.py` (12 tests).
- Fixture tests for all 7 kinds, including the FC1 kind correction and the default-SA warning:
  - Covered by `test_discover.py` (20 tests) over 18 fixtures in `console/tests/fixtures/discover/`.
  - `test_preflight.py` (9 tests) covers 5 scenarios.
  - `test_configure_api.py` (9 tests) covers the store and the API.
- Checked in headless Chromium in fixture mode:
  - A config made as Premium was corrected to Flex by Discover, and the pull path was turned off.
  - The Preflight checklist rendered; Save, then Simulate this config, gave a PASS verdict.
  - No console errors. This found and fixed a stray "null" text bug that dated from M2.
  - Screenshot: `docs/console-m3-configure.png`.
- **Not yet done: one run against a real non-prod project and subscription.** See Open / deferred.

**Tests**: client 28, rotator 8, console 134 (50 of them new in M3), scenarios 13/13.

**Demo**

```bash
PYTHONPATH=client/src:rotator:. python -m console --fixtures console/tests/fixtures   # no cloud access needed
AZURE_CONFIG_DIR=$HOME/.azure-personal PYTHONPATH=client/src:rotator:. python -m console   # live, read-only
```

## M4 Terraform generation, plan/apply gates, wire, Runs tab

**Built**

- `console/steps/tfgen.py`: generates shared workspaces.
  - Key: `gcp:<project>:<region>` or `azure:<subscription>`, under `.console/workspaces/` (0700; files 0600).
  - `console.json` holds the targets and shared settings.
  - `main.tf.json` is built from module constants only and references `var.*`.
  - Operator data goes only to `terraform.tfvars.json`; `backend.tf.json` is local, gcs or azurerm.
  - Locking: a global render lock plus a per-workspace lock that also covers every terraform operation.
  - Render refuses conflicts:
    - shared settings,
    - pull settings,
    - the same repo with another upstream,
    - the same integration or mapping name in another workspace on one JFrog URL,
    - the same GCP `name_prefix` in two regions of one project,
    - a pull path without its inputs.
- `console/steps/terraform.py`: `plan -out=plan.bin` → `show -json` summary (create/update/replace/delete,
  destructive addresses) + the plan file's sha256. `apply` runs `apply plan.bin` only when:
  - the operator approved that sha256;
  - the file on disk still hashes to it (checked again under the lock);
  - for delete/replace, the workspace name was typed.
  - Every refusal happens before a subprocess starts.
  - The plan file is single-use.
  - `JFROG_ACCESS_TOKEN` reaches terraform via its environment only.
  - `show`/`output` run as sensitive calls, and sensitive outputs are masked.
- `console/steps/wire.py`: diff of the managed settings only, current vs desired, plus the exact commands. The
  sha256 covers both.
  - Apply re-reads the target and refuses with `target_changed` if it moved, then runs the commands and re-reads
    to verify.
  - GCP: `gcloud run services|jobs update --update-env-vars`.
  - Container Apps: `az containerapp update` / `secret set keyvaultref` / `registry set`.
  - App Service / Functions: `functionapp update keyVaultReferenceIdentity` and `config appsettings set`. App
    settings are read as a sensitive call and filtered to managed keys.
  - Each plan also includes notes and an IaC snippet.
- `console/runner.py`: the 9-step pipeline:
  1. preflight
  2. discover
  3. simulate
  4. plan
  5. apply (approval gate)
  6. wire (approval gate)
  7. first rotation, 8. verify, 9. report: skipped until M5.
  - State machine with cancel.
  - Events are redacted and written to `runs/<id>/events.jsonl`; `run.json` is written at the end.
  - A no-change plan applies without asking; wire loops back on `target_changed`.
- Server: the `/api/runs` routes:
  - `GET`/`POST /api/runs`
  - `GET /api/runs/{id}`
  - `/events` (SSE)
  - `/approve` (`plan_sha256`, `typed_confirmation`, `diff_sha256`)
  - `/cancel`
  - Errors carry a `code` (`not_approved`, `plan_hash_mismatch`, `plan_file_changed`,
    `typed_confirmation_required`, `diff_hash_mismatch`, `target_changed`).
- UI:
  - **Runs** tab (`static/runs.js`):
    - run list and step stepper;
    - per-step evidence: preflight checks, discover facts, simulate verdict, plan table with destroys in red,
      wire diff/commands/notes/IaC;
    - inline approvals, with typed confirmation on destructive plans;
    - two-click cancel and a live log.
  - Configure gains **Run pipeline** and fields for the rotator image, name prefix, ACR ID and ACA environment ID.
  - The run log is collapsed under "Show details" by default.
  - The session token is kept in `localStorage` for the console's origin, so reloads and new tabs work. A restarted
    console issues a new token; the old one is rejected and dropped. Before, it lived only in page memory, and a
    reload locked the operator out.
- Terraform module: `azure-identity-and-pull-path` gains `pull_path_enabled`. The pull inputs default to null;
  every pull resource is count/for_each-gated and has `moved` blocks to `[0]`, plus a precondition. It validates
  with the pull path on and off.
- `terraform fmt -recursive -check` passes. The four files carried over from M3 were formatted.
- M3 follow-ups from the live read-only run:
  - Discover maps the real gcloud wording ("Cannot find service") and a disabled Cloud Functions API.
  - Discover maps an unregistered Azure resource provider.
  - Preflight checks the enabled GCP APIs and the registration state of the Azure providers, and shows the
    enable/register command. It does not run it.

**Acceptance**

- `terraform validate` passes with real providers for:
  - `terraform/examples/complete`;
  - generated workspaces of all 7 kinds;
  - an Azure workspace with the pull path off.
  - Run it with `CONSOLE_TF_VALIDATE=1 … -m unittest console.tests.test_tfgen`.
  - Locked versions: google 8.9.0, azurerm 5.12.0, azapi 2.13.0, azuread 3.10.0, jfrog/platform 2.2.11,
    random 3.9.1; Terraform 1.15.5.
- The fake terraform (`console/tests/fixtures/fake_terraform`) covers the gates. Apply is refused when:
  - it has no approval;
  - the plan hash doesn't match;
  - the plan file changed or was already used;
  - a delete/replace has no typed confirmation.
  - Wire is refused when the diff hash doesn't match or the target changed.
- Browser, replay demo (`fixtures/demo`): Run pipeline →
  - preflight/discover/simulate/plan succeed;
  - Apply waits for approval → approve → Wire shows a 4-setting diff → approve → applied and verified;
  - no console errors.
  - Screenshot: `docs/console-m4-run.png`.
- Redaction test covers the new log paths: run events, `events.jsonl` and the `run.json` snapshot.

**Tests**: client 28, rotator 8, console 213 (2 opt-in validate tests skipped by default; 79 new in M4),
scenarios 13/13.

**Demo**

```bash
PYTHONPATH=client/src:rotator:. python -m console --fixtures console/tests/fixtures/demo   # full pipeline, no cloud access
```

Configure → name `orders`, project `demo-nonprod-1234`, region `us-central1`, service `orders`, any rotator
image → **Run pipeline**.

**M3 live read-only run (done this milestone)**

- GCP `csb-test-257615`:
  - ADC ok;
  - 18–19 of the probed permissions granted;
  - Cloud Functions API disabled;
  - no workloads.
- Azure `cra-ac-sub`:
  - Owner on `cra-ac-sub-rg`;
  - `Microsoft.App` and `Microsoft.KeyVault` not registered;
  - no apps or vaults.
- Neither account has a workload, so Discover could only report "not found". It reported that correctly after the
  wording fixes above.

## AKS pilot lessons (2026-10-29)

Lessons from the AKS sandbox kubelet-provider pilot, carried over. `docs/aks-pilot-lessons.md` has the full mapping.

- **Docker pulls go to the repo's dedicated host** (`<server>-<repo-key>.jfrog.io`), not the platform host. New
  config fields `jfrog.docker_repo` / `jfrog.docker_host` (`model.docker_host`). Wire uses that host as the registry
  server. GCP Docker remotes default to it (an empty `upstream_uri`). tfgen refuses a Docker upstream with a path
  and warns on the platform host. The example and the gcp module docs and validation follow.
- **`jfrog_url` must be an origin**: client (`ValueError`), model (`jfrog_url_origin`), preflight, tfgen, and each
  Terraform `jfrog_url` validation.
- **Preflight fails** when the integration name exists with a different `issuer_url`.
- **Mapping rules**:
  - runtime TTL ≤ 3600 s (model error; module validation);
  - exact subjects, no wildcards (module);
  - one mapping per identity (module; tfgen refuses two targets on one subject).
- `terraform/examples/gcp-cloud-run` (added in the same evening's live run): the AR remote now points at
  `https://<server>-<docker_repo>.<domain>` and the demo image drops the repo key from its path. The live run's AR → JFrog
  403 used the platform host. Whether the repo host changes that is **untested**: re-run the pull demo with it before
  opening the JFrog support case.
- `rotator/Dockerfile` takes `BASE_IMAGE`.
- README: Docker host section; verify adds uncached pulls and audit attribution.

**Tests**: client 29, rotator 8, console 225 (12 new or changed), scenarios 13/13.
Not run: `terraform validate` / `fmt`. The provider registry is not reachable from the session that made the change;
run `CONSOLE_TF_VALIDATE=1 … -m unittest console.tests.test_tfgen` and `terraform fmt -recursive -check terraform/`
before merging.

## Open / deferred

- **No real `plan`/`apply` has run.** It needs `JFROG_ACCESS_TOKEN`, a deployed workload and your explicit go-ahead
  (approval-gated). The first real apply will also settle the remaining CLAUDE.md unverified items.
- GCP alert notification channels and the Azure alert's action group are not exposed in the console config yet.
- Bicep/ARM snippets for the wire step are deferred. The IaC snippet is a JSON settings map.
- The azuread provider takes its tenant from the az CLI default. Pass `AZURE_CONFIG_DIR` for an isolated login.
- `runtime.username` is not used for mappings: the module's runtime mappings are group-scoped, so the token has no
  user.

- **Samples untested.** `/healthz/jfrog` in both samples needs `functions_framework` / `azure.functions`, which are
  not available under the stdlib-only rule. `probe.selftest` itself is tested. Verify on first deploy (M5).
- **Preflight limits:**
  - The GCP permission list per kind is a best estimate until M4 applies for real.
  - Entra app-registration rights are not checked (reported as skip).
  - The production check is a name heuristic only.
  - JFrog checks are skipped while `JFROG_ACCESS_TOKEN` is unset.
- **Simulation fidelity.** The emulators follow documented behaviour; they are not recordings of the real services.
  The unverified points below (AR `updateMask`, the Functions-on-ACA API version, Flex argument names, the
  `jfrog/platform` schemas, the ACA log table) are not exercised by the simulator at all, so a passing simulation
  does not prove them.
- CLAUDE.md "Known unverified items": Flex argument names and the platform schemas are now verified by validate; the rest remain.
- `webbrowser.open(url)` passes the token URL to the OS browser launcher, so it is briefly visible in that process's
  argv on this machine. Use `--no-browser` on shared hosts.
- `HEAD` requests get 501 (only GET/POST are implemented). Harmless; noted for M6 hardening.

## Live end-to-end run, GCP (2026-10-30)

Test accounts: JFrog Cloud trial `trial9hnw59.jfrog.io`, GCP `csb-test-257615`.
New: `terraform/examples/gcp-cloud-run` (GCP-only example; two applies) and `samples/cloud-run-job`.

- JFrog via API: group `serverless-readers`, `docker-remote` (Docker Hub), `demo-generic` with `hello.txt`,
  permission `serverless-read`, no-permission user `svc-gcp-ar-upstream`.
- Terraform apply 1: 26 resources (OIDC integration, 2 mappings, AR remote, secret, rotator job + scheduler, IAM).
- **Runtime path works**: Cloud Run job `jfrog-runtime-demo` exchanged its Google identity for a 1 h JFrog token
  and read `demo-generic/hello.txt`.
- **Rotator exchange works** (4 h token, group-scoped, user `svc-gcp-ar-upstream`); the AR repoint fails:
  Artifact Registry gets 403 from JFrog Cloud's edge for every credential, including anonymous. The requests are
  absent from Artifactory's request log; the same credentials return 200 from a laptop and from Cloud Build.
  Needs JFrog support to allow AR traffic. `updateMask` confirmed correct.
- Remote repos also need **Deploy/Cache** for the pull user to fetch uncached images (read alone gives 403).
- Next: Azure Container Apps, where the platform pulls from JFrog directly with the rotated Key Vault token.

## Console UI: one cloud per page (2026-10-30)

- `?cloud=gcp` and `?cloud=azure` are separate pages; `/` asks which cloud. Each page
  shows only its own app types, fields, saved setups, runs and simulation scenarios.
- Set up tab is three steps (Your app, JFrog, What the app needs) plus collapsed
  Advanced settings; Discover is "Find app", Preflight is "Check access", Run
  pipeline is "Set it up". Problems are listed by field label, after you type or
  press a button.
- Sign-in sticks: the session token is kept in `.console/session-token` (0600,
  git-ignored) across restarts (`--new-token` replaces it); `localhost` redirects to
  `127.0.0.1` so the browser keeps one saved sign-in. Top-level page loads from
  another site get the static shell; the API still refuses them (test added).

## Images from Artifact Registry / ACR; JFrog for artifacts only (2026-10-30)

- Direction: images live in Artifact Registry or ACR and the platform pulls them with
  its own identity. JFrog serves artifacts (files, packages) to the app's code through
  the runtime exchange. The JFrog image-pull path stays in the modules but is opt-in.
- `terraform/examples/gcp-cloud-run` is now runtime-only (JFrog trust, one mapping,
  the demo job). Applied live: 23 pull-path resources destroyed (rotator, scheduler,
  secret, AR remote, IAM, metric); APIs left enabled (`disable_on_destroy = false`).
  Re-ran `jfrog-runtime-demo`: token valid 3599 s, read `demo-generic/hello.txt`.
- Also found: the trial instance does not serve `<server>-<repo>.jfrog.io` (it redirects
  to landing.jfrog.com/reactivate-server), so the subdomain method is not available there.
- Console: new setups start with the image pull off.

## Console: adopt existing integrations, plain plan summary, brand palette (2026-10-30)

- Check access marks an OIDC integration that already exists for the same issuer as
  reused (`jfrog.adopt_integration`); the generated workspace imports it with a
  Terraform `import` block (`for_each`, so Terraform >= 1.7) instead of failing on a
  duplicate. A different issuer still fails. Live: the GCP run's plan imports
  `gcp-cloudrun` and creates one mapping.
- Plan summary counts imports and reads as one sentence; the greyed count chips are gone.
- brand palette: kelly green #5BA908 (PMS 369 C, from third-party brand listings; no
  official design system found) for brand marks, #3E7A0A for buttons/links (5.3:1 on
  white), #7CC73A in dark mode. Active tab and cloud switch are green; kind tags are
  plain labels. The unsaved-changes leave-page prompt is gone.

## Console design pass and technical wording (2026-10-30)

- Configure tab: forest header band, the three steps on a rail (they are a sequence),
  and a sticky side panel with the OIDC trust chain (workload identity, issuer, OIDC
  integration, identity mapping, access token) and the planned changes by Terraform
  resource type. Avenir Next, 16px base, custom selects, AA-contrast greens.
- Wording uses the real terms: Configure, Discover identity, Preflight, Run pipeline,
  Target workload, Token paths (runtime token exchange, image pull path), Reader group.
- Deleting a saved configuration deletes its finished runs (memory and .console/runs);
  active runs are kept. The Delete button shows only while a configuration is loaded.
- Simulate: fault and scenario names in plain words; each cloud page lists only its faults.
- Run results read as sentences (applied / nothing to change / checked live).

## GCP reinstall and three live patterns (2026-10-29)

Tore down every GCP and JFrog resource from earlier runs, then reinstalled from
`terraform/examples/gcp-cloud-run` and the console. All three patterns passed live:

- **New workload (Terraform):** `jfrog-runtime-demo` job, image from AR `platform`,
  the example sets the 4 env vars and the `gcp-run-demo` mapping. The job exchanged its
  ID token for a 3599 s token and read `demo-generic/hello.txt`.
- **Existing workload (console):** `orders-legacy` job created by hand with no JFrog
  config failed with "missing environment variables". Console run: preflight adopted the
  existing `gcp-cloudrun` integration (import 1, create 1 mapping), wire set the env vars
  and verified them. The unchanged job then read the artifact.
- **Package build (Cloud Build):** `samples/cloud-build-pip` runs as `jfrog-oidc-build`
  (mapping `gcp-build`) and pip installs from `pypi-remote` with a 1 h token in
  `PIP_INDEX_URL`. Findings, now in the example's Terraform:
  - The build SA needs `roles/iam.serviceAccountOpenIdTokenCreator` on itself; without
    it the metadata identity endpoint returns 404 "please provide a user-specified
    service account". The grant takes a few minutes to propagate.
  - It needs `roles/storage.objectViewer` on `<project>_cloudbuild` to fetch the source.
  - JFrog: READ alone on a remote repo can't fetch uncached packages; the group needs
    WRITE (deploy/cache) on `pypi-remote` (separate permission `serverless-remote-cache`).
- Runs: steps that aren't built yet (first rotation, verify, report: M5) carry
  `planned: true` and are hidden in the run view instead of showing as skipped.

## Images from JFrog without the AR remote: build-time mirror (2026-10-29)

The AR remote → JFrog route is still blocked on the trial (403 at JFrog's edge; the
MyJFrog IP allowlist is empty, i.e. unrestricted, and geolocation rules aren't in this
plan). Google's network itself reaches JFrog fine, so images come in at build time:

- `samples/cloud-build-image-mirror`: the build SA exchanges its ID token for a 1 h
  JFrog token, `docker login`s by repository path (`<host>/<repo>/<image>`, no Docker
  subdomain needed), pulls, pushes to AR `platform`, deletes the token, logs out.
  Cloud Run deploys from AR as before. No secret, no rotator; JFrog still gates
  every image.
- Verified live: `docker-remote/library/alpine:3.20` → `platform/alpine:3.20`.
- Needs: `roles/artifactregistry.writer` on `platform` for the build SA (example
  Terraform `build_push`; granted with gcloud first, the iam_member converges) and
  WRITE (cache) on `docker-remote` for `serverless-readers`
  (`serverless-remote-cache` now covers `pypi-remote` and `docker-remote`).
- Example output `image_mirror_command`.

## CI/CD is Azure DevOps only (2026-10-29)

Decision: build and deploy run in Azure DevOps for both Azure and GCP targets.
Cloud Build and GitHub Actions options are removed for now:
- Deleted `samples/cloud-build-pip`, `samples/cloud-build-image-mirror`, `.gcloudignore`.
- Example: removed the Cloud Build SA (`jfrog-oidc-build`), its grants and the
  `gcp-build` JFrog mapping (applied: 5 destroyed; AR writer binding and the
  mirrored `platform/alpine` image removed with gcloud).
- What carries over to ADO, proven live with Cloud Build: package installs and the
  JFrog → AR image mirror with a 1 h OIDC token; WRITE (cache) on remote repos for
  the reader group (`serverless-remote-cache`: `pypi-remote`, `docker-remote`).
