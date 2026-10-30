# Plan: one UI for the JFrog OIDC console and the KCP console

Status: done (2026-10-30). Run `python -m portal`; KCP seam a14fab8.

## Goal

One local process, one port and one sign-in, with **two strictly separate sections**:

```
/                         landing: two cards, Serverless or Kubernetes. Nothing else.
SERVERLESS  /serverless/  console/       Cloud Run, Container Apps, Function Apps
            Google Cloud  /serverless/gcp/{configs,configure,runs,simulate}
            Azure         /serverless/azure/{configs,configure,runs,simulate}
KUBERNETES  /k8s/         kcp_console/   kubelet credential provider on clusters
            Azure AKS     /k8s/clusters/aks/...
            Google GKE    /k8s/clusters/gke/...
            AWS EKS       /k8s/clusters/eks/...
            OpenShift     /k8s/clusters/openshift/...
```

Both consoles still run on their own (`python -m console`, `python3 -m kcp_console`), and
their behaviour there doesn't change: each seam defaults to `base=""`.

## Separation rules

- **URLs:** every serverless page, API call and static file lives under `/serverless/`, and every KCP one under `/k8s/`.
  Nothing is shared at the root except the landing page and `/favicon.ico`. The old paths redirect:
  `/gcp*` and `/azure*` go to `/serverless/...`, and `/clusters*` goes to `/k8s/clusters/...`.
- **Code:** the portal dispatches by prefix and holds no product logic. `console/` and `kcp_console/` never
  import each other; only `portal/` imports both.
- **State:** separate stores (`.console/` and `.kcp-console/`), configs, runs, inventory caches, simulators and
  approval gates. A serverless config never appears in KCP, and the other way round.
- **UI:** the sidebar has two labelled sections. The section you're in is expanded and highlighted, and the other
  shows only as a single link to switch over. The topbar shows the current section ("Serverless" or "Kubernetes").
  Pages inside a section show nothing from the other.
- **Inside each section:** the current per-cloud and per-platform separation stays (GCP and Azure; AKS, GKE, EKS
  and OpenShift).
- **Shared:** only the session token (one sign-in), the security headers and the CSS design tokens (KCP's `:root`
  tokens: `--fs-*`, `--s1..s6`, `--r-*`, `--sh-*`, `--ctl-h`, color roles).

## Design

1. **New `portal/` package** (stdlib only), run with `PYTHONPATH=client/src:rotator:. python -m portal [--port 8760]`.
   - It builds a `ConsoleServer(base="/serverless")` and a `KcpServer(base="/k8s")` with the **same token**,
     without starting their HTTP servers. It sets `port`, `host` and `static_files` on each so that
     `allowed_hosts()` matches the portal's port.
   - One `ThreadingHTTPServer` routes by prefix, removing it before handing the request on:
     `/serverless/*` goes to the serverless handler, `/k8s/*` to the KCP handler, `/` to the landing page,
     and the old paths get a 308 to the new ones.
   - Both handlers keep their own Host, Origin and token checks unchanged.
   - It uses the same CLI flags as both mains: `--no-browser`, `--new-token`, `--print-url`, `--fixtures`,
     `--kcp-fixtures`, `--local-apply`, the loopback bind check, and `pipelines/delivery.json`.
   - Stopping it stops both consoles' run managers and simulation workers.
2. **The same seam in both consoles**, `base=""` by default:
   - The server injects `<meta name="console-base" content="/serverless">` (or `/k8s`) into `index.html` and
     rewrites its `/static/` links under the base.
   - The JS reads `BASE` once. `api()` and stream fetches use `BASE + path`. The router strips `BASE` before
     matching `location.pathname` and adds it back on `pushState` and link `href`s.
   - `index.html` has a hidden "other section" link (Serverless shows "Kubernetes →", KCP shows "Serverless →"),
     shown only when `BASE` is set. The section title in the topbar comes from the meta.
3. **Who does what:** this session handles `console/` and `portal/`; the KCP owner session handles `kcp_console/`,
   using the seam spec above.

## Tests first

- `portal/tests/test_portal.py`:
  - every `/serverless/*` path reaches only the serverless console, and every `/k8s/*` path reaches only KCP
    (checked by a marker in the response);
  - `/api/*` at the root is 404;
  - the old paths get 308s;
  - one token works for both, and a missing token gets 401 on each prefix;
  - a bad Host or Origin is refused on each prefix;
  - the meta and static rewrites are applied;
  - replay mode works with both fixture sets;
  - a serverless config saved through the portal doesn't show up in `/k8s/api/configs`, and the other way round.
- Both existing test suites still pass unchanged (standalone default).
- The portal's log path goes through redaction and is added to the redaction test.
- Browser check: the landing page, each section, switching sections while signed in, and deep links reloading
  correctly under each prefix.

## Order

1. Wait until the serverless session (`-61`) has finished and pushed. KCP (`-8d`) is done (01c91ea).
2. Build the serverless seam and the portal (this session), with tests. In parallel, the KCP seam (`-8d`).
3. Update CLAUDE.md with `python -m portal`, then commit and push by path.
