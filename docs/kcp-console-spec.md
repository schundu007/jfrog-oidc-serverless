# KCP Console: build spec

A local web console (`python -m kcp_console`, `http://127.0.0.1:8766/#t=<token>`)
that configures, simulates, installs, verifies, troubleshoots, demos and rolls
back the **JFrog kubelet credential provider** ("KCP") on AKS, with no
`imagePullSecrets` and no stored JFrog credentials.

It is the Kubernetes sibling of `console/` (spec `docs/console-spec.md`) and
reuses its security model, shell/redaction/store patterns and look. The pilot
runbook ("JFrog Kubelet Credential Provider — sandbox Runbook", last updated
2026-10-16) is the knowledge it encodes. Every runbook fact lives in one of
these places, and `docs/kcp-progress.md` §Traceability says which:

- `kcp_console/knowledge.py`: the runbook's tables and texts as data. The UI and
  the report render them. The UI does not hard-code runbook prose.
- `kcp_console/model.py`: defaults (`sandbox-pilot`), inputs and validators.
- `kcp_console/logic.py` and `kcp_console/parse/`: decision logic and parsers,
  shared by the real checks and the simulator.
- `kcp_console/checks/`, `kcp_console/diagnose.py`: checks and the troubleshooting
  rule engine.
- `kcp_console/sim/scenarios/*.json`: simulator scenarios.

## 1. Scope

- **Supported:** AKS, Option B (the cluster's projected ServiceAccount token goes
  straight to Artifactory Access; no Azure AD app).
- **Modelled, not built:** Option A (nodepool managed identity plus an Azure AD
  app registration). The Configure tab shows what A would need, because the
  security review needs a documented A-vs-B decision.
- **Seam for later:** EKS, GKE, OpenShift (`model.PLATFORMS`; only `aks` is
  enabled). The upstream README lists them as supported.
- **Modes:** *pilot* drives `argocd-utility` with its `TestScenario` names;
  *template* drives `jfrog-kcp-cluster.yml` (`Action`) and
  `jfrog-kcp-mappings.yml`.
- **Execution model.** Personal accounts have *AKS Cluster User* and *RBAC
  Reader*: read-only, namespaced objects only (Nodes are cluster-scoped and
  excluded). So the console:
  - never runs an ad-hoc state-changing `kubectl` or `helm` (enforced in `k8s.py`: only
    `get`/`auth can-i`/`version`; helm only `get`/`list`/`history`/`status`);
  - where the operator has the rights, runs jfrog/jfrog-credentials-provider AZURE.md
    Option B directly (`direct.py`: setup, verify, teardown) as one approved run: the
    approval screen lists every command and request, the request hash binds them,
    DryRun is the default (`--dry-run=server`; az and JFrog writes shown, not sent),
    and teardown needs the cluster name typed in;
  - queues Azure DevOps pipeline runs (`az pipelines run`) with parameters using
    the operator's `az login`, and streams and stores their logs;
  - uses local `kubectl` for reads personal RBAC allows, and on
    `Forbidden ... User does not have access to the resource in Azure` points to
    the read-only pipeline scenario instead;
  - calls Artifactory REST directly for reads, with `JFROG_ACCESS_TOKEN` from the
    environment. JFrog writes are approval-gated.

## 2. Ground rules

Same as `console/`:

- Python stdlib only; the UI is vanilla ES modules (no build, CDN or external
  fonts).
- Server: bind `127.0.0.1` (other hosts need `--i-know-this-exposes-credentials`);
  32-byte session token in the URL fragment `#t=`, sent as `X-Console-Token`,
  compared with `hmac.compare_digest`; Host and Origin/`Sec-Fetch-Site`
  allow-lists; strict CSP (no inline script or style, `frame-ancestors 'none'`,
  `form-action 'none'`), nosniff, no-referrer, DENY framing,
  `Cache-Control: no-store`; JSON-only POST bodies ≤ 1 MiB; static allow-list.
- Secrets are never printed, logged, stored or returned: JFrog access and admin
  tokens, `JFROG_ACCESS_TOKEN`, `JFROG_DOWNLOAD_TOKEN`/`JfrogDownloadToken`,
  `ARTIFACTORY_ADMIN_TOKEN`, projected SA tokens and any JWT, Key Vault values
  (`artifactory-download-token`), ADO PATs and `Authorization` headers. Everything
  goes through `redact.py`; `tests/test_redact.py` feeds known secrets through
  every log path.
- Subprocesses only through `shell.py` (argv lists, no shell, timeout, env
  allow-list, stdin closed; modes `live`/`replay`/`record`).
- Approval gates: a pipeline run with `DryRun=false`, any scenario that changes
  state, any JFrog write. An approval binds to sha256 of the canonical request
  (pipeline, branch, parameters, values-file hash). Destructive actions also need
  the cluster name typed in. Dry run is the default everywhere.
- Tests first, with recorded fixtures for `az`, `kubectl`, ADO logs and
  Artifactory. No test needs cloud access.
- Configs: `.kcp-console/configs/<name>.json` (0600 in a 0700 directory), no
  secrets; a config `redact` would change is refused.

## 3. Layout

```
kcp_console/
  __main__.py      python -m kcp_console [--port 8766] [--no-browser] [--fixtures DIR] [--record DIR]
  server.py        ThreadingHTTPServer, security, JSON API, SSE
  shell.py         argv-only subprocess + HTTPS client; live/replay/record
  redact.py        secret scrubbing
  store.py         configs, runs (run.json + events.jsonl), status (open items, security rows, demo), reports
  model.py         config schema, sandbox-pilot defaults, validators
  knowledge.py     the runbook as data
  logic.py         token URL, MergeFiles, matchImages, reset safety (old and fixed), cache window, estimates
  parse/           yamlish, kubelet_config, plugin_log, journal, events, helm, summary (QUICK SUMMARY)
  checks/          preflight, three_layers, writers, kubelet_restart, verify, audit_attribution,
                   orphaned_mappings, chart_rbac, binary_integrity, token_scope, log_leaks, values
  diagnose.py      the troubleshooting rule engine
  ado.py           queue/poll/fetch logs for argocd-utility, jfrog-kcp-cluster, jfrog-kcp-mappings
  k8s.py           read-only kubectl/helm
  jfrog.py         Artifactory/Access reads; writes only via approved actions
  runner.py        run state machine: prepare → approve (hash, typed name) → queue → poll → parse
  report.py        Markdown/JSON report
  sim/             world, engine, scenarios/*.json, __main__ (list|run|check-all)
  static/          index.html, styles.css, app.js + one module per tab, svg.js diagrams, timeline.js
  tests/           fixtures/ + unit tests
```

## 4. Data model (`model.py`)

A config is JSON:

```
name, mode: "pilot"|"template", platform: "aks", option: "B"
cluster: {name, resource_group, subscription_id, subscription_name, oidc_issuer, environment: SBX|NPE|PRD}
k8s: {namespace, service_account, release, daemonset, test_pod, stale_pods[], node_label}
jfrog: {host, oidc_provider, mapping_name, mapped_user, scope, expires_in, protected_providers[],
        test_repo, test_repo_host, test_image, binary_repo, promote_source_repo}
values: {artifactory_url, match_images[], default_cache_duration, provider_name, azure_app_audience,
         token_attributes_enabled, download_url, chart_repo, chart_name, chart_version, binary_version,
         rbac_create, node_affinity_label}
template: {MatchImages, BinaryMirrorRepo, ArtifactoryHost, OidcProviderName, DefaultCacheDuration,
           MaxUnavailable, Nodepools, VerifySampleNodes, TestImage, ResetStaleNodeEntries,
           ChartVersion, BinaryVersion}
ado: {organization, project, branch, repo, pilot_pipeline, cluster_pipeline, mappings_pipeline,
      service_connection, key_vault, key_vault_secret, values_path}
overrides: {DiagnosticImage, ChartRepo}
```

The `sandbox-pilot` default carries every value from the runbook's environment
reference. Validators (`model.validate`) return `{level, field, message}`
items; errors block Install and Run. Each rule has a test.

## 5. Pipeline (`runner.py`, `ado.py`)

A run is one pipeline invocation:

1. **prepare**: build `{pipeline, branch, parameters, values_sha256}`; check the
   scenario/action registry (`knowledge.PILOT_SCENARIOS`, `TEMPLATE_ACTIONS`);
   apply guards (demo mode, protected providers, PRD gate, secret variable
   never in parameters); compute `request_sha256`.
2. **approve**: needed when the request changes state (the scenario's column, or
   `DryRun=false`). The approval must carry the same `request_sha256`;
   destructive requests also need the cluster name typed in.
3. **queue**: `az pipelines run --name <p> --branch <b> --parameters k=v ... --org --project -o json`.
4. **poll**: `az pipelines runs show --id <id>` until completed.
5. **logs**: `az pipelines runs show` gives the build id; the log text comes from
   the ADO REST `_apis/build/builds/<id>/logs` with a bearer from
   `az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798`
   (sensitive: never logged or recorded).
6. **parse**: QUICK SUMMARY first, full log as fallback; derive evidence; run
   checks and the rule engine; flag `Forbidden` lines whose identity is a user
   principal; scan for unmasked tokens.

Template mode: `JfrogDownloadToken` is a secret pipeline variable. The console
never puts it in parameters; it checks that `az pipelines variable list` reports
it `isSecret: true` and tells the operator otherwise.

## 6. Verification (`checks/`)

| Check | Pass rule |
|---|---|
| `preflight` | OIDC issuer and Workload Identity enabled; issuer captured; foreign JFrog DaemonSets listed |
| `three_layers` | values file, `helm get values -a`, manifest, node entry and newest plugin URL agree; exactly one `https://` in the newest `RT oidc token url` |
| `writers` | only the release's DaemonSet mounts the provider config path; an orphaned JFrog DaemonSet elsewhere is critical |
| `kubelet_restart` | `Kubelet started at` later than `Config file mtime` |
| `verify` | `Successfully pulled` → PASS; `already present on machine` → INCONCLUSIVE; anonymous-token 401 / BackOff → FAIL; plus a fresh-exchange note |
| `audit_attribution` | the Artifactory access log for the pull window names the mapped user |
| `orphaned_mappings` | each mapping's `sub` names a namespace/SA that exists |
| `chart_rbac` | lists every verb/resource the chart's Role/ClusterRole grants |
| `binary_integrity` | mirrored size ≥ 1 MB and near 14,359,603; sha256 vs upstream when reachable, else "unverified" |
| `token_scope` | read-only `GET /access/api/v2/groups`; reports what the token can do |
| `log_leaks` | stored run logs contain no unmasked token patterns |
| `values` | the values validators |

## 7. Server (`server.py`)

Same security as `console/server.py`. API (all JSON, all need the token):

- `GET /api/session`, `GET /api/knowledge`
- `GET/PUT/DELETE /api/configs[/<name>]`, `POST /api/validate`, `POST /api/values` (render + diff)
- `POST /api/discover`, `POST /api/preflight`
- `GET /api/sim/scenarios`, `POST /api/sim`, `GET /api/sim/<id>`, `GET /api/sim/<id>/events` (SSE)
- `POST /api/runs` (prepare), `POST /api/runs/<id>/approve`, `POST /api/runs/<id>/cancel`,
  `GET /api/runs`, `GET /api/runs/<id>`, `GET /api/runs/<id>/events` (SSE)
- `GET /api/nodes?config=`, `POST /api/diagnose`, `GET /api/artifactory?config=`,
  `POST /api/artifactory/<probe|aql|storage>`, `POST /api/verify/audit`
- `GET/PUT /api/status/<config>` (open items, security rows, demo checklist and notes)
- `GET /api/report/<config>?format=md|json`

## 8. Simulation (`sim/`)

Emulates behaviour, not binaries, on a seeded virtual clock:

- kubelet: reads the config once at start; `matchImages` host matching; plugin
  result cached for `defaultCacheDuration`; per-node image cache.
- plugin: token URL `"https://" + artifactory_url + "/access/api/v1/oidc/token"`;
  `MergeFiles` replaces an entry only on identical `artifactory_url`; logs the
  runbook's exact lines.
- containerd: `failed to fetch anonymous token ... 401` with no credential.
- DaemonSet setup step: writes the entry and restarts kubelet; an orphaned
  writer reverts the file.
- Artifactory: OIDC token endpoint (provider, issuer, `sub`, mapping), repo
  permissions, registry v2 path method vs dedicated subdomain, Promote local vs
  virtual, dormant remote (302 to `reactivate-server`), audit log.

The simulator renders the same evidence formats real runs produce (pod events,
QUICK SUMMARY, plugin log, node file, helm values/manifest), then runs the
console's own parsers, checks and rule engine on them. Each scenario has an
expected verdict, root-cause rule and causal-chain keywords. CLI:
`python -m kcp_console.sim list|run <name>|check-all`.

## 9. UI (`static/`)

Tabs: Overview, Configure, Install, Nodes, Artifactory, Verify, Troubleshoot,
Simulate, Demo, Security, Rollback, Runs. Content per tab follows §12.2 of the
build prompt; texts come from `/api/knowledge`. SVG diagrams (before/after,
Option B flow, node, pipeline) and a swim-lane timeline. Light/dark via
`prefers-color-scheme`; 1024–1280 px; keyboard navigable.

## 10. Milestones

- **M0** spec, pure parsers and logic, fixtures from the runbook strings.
- **M1** simulator, all scenarios, `check-all` green.
- **M2** server, Overview and Simulate tabs; security tests; diagrams.
- **M3** Configure, Discover, Preflight, values diff (replay fixtures).
- **M4** ADO execution and Runs (approval hash, typed confirmation, secret vars).
- **M5** Nodes, Verify, audit attribution, Troubleshoot, Artifactory tools, Mappings editor.
- **M6** Demo, Security, Rollback, report export, hardening.

Commit and push to `main` after each. Nothing runs against real Azure, ADO or
JFrog during development; the first live use is a read-only Preflight and
Discover on sandbox.

## 11. Definition of done

- Every traceability row in `docs/kcp-progress.md` is ticked with its file or test.
- All tests and simulator scenarios pass.
- `python -m kcp_console --fixtures kcp_console/tests/fixtures/sandbox` shows the
  pilot story (the doubled URL, the stale node entry, the reset, the pull of
  142,944,877 bytes in 10.487 s), the open items, and no console errors.

## 12. Unverified assumptions in the console itself

- The QUICK SUMMARY footer and plugin-log JSON layout in the fixtures are
  reconstructed from the runbook's field names, not copied from a pipeline log.
  The parsers are tolerant (key/value lines, JSON or plain plugin lines). Fix the
  fixtures with the first recorded live run.
- The Access API calls for listing, updating and deleting mappings, and the
  request-log query used for audit attribution, have not been exercised.
- The Promote body is the documented shape, not the pilot's saved payload.
