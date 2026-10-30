# Plan: one UI for the JFrog OIDC console and the KCP console

Status: planned, not started. It starts once the serverless (`console/`) and KCP
(`kcp_console/`) sessions have finished their current work.

## Goal

One local process, one port, one session token and one sidebar:

```
Serverless   Google Cloud   /gcp/...                (console/)
             Azure          /azure/...
Clusters     Azure AKS      /clusters/aks/...       (kcp_console/)
             Google GKE     /clusters/gke/...
             AWS EKS        /clusters/eks/...
             OpenShift      /clusters/openshift/...
```

Both consoles still run on their own (`python -m console`, `python3 -m kcp_console`), and
their behaviour there doesn't change.

## Why it is small

- The page routes are already separate: serverless uses `/`, `/gcp/*` and `/azure/*`; KCP uses
  `/clusters/*` and `/configure`.
- The two consoles overlap only on `/api/*` and `/static/*`.
- Both use the same security model (127.0.0.1, `X-Console-Token`, Host/Origin checks,
  `frame-ancestors 'none'` CSP), so a single origin keeps every check as it is. No iframes and no proxy.
- They keep their tokens under different localStorage keys (`console-session-token` and
  `kcp-console-session-token`), so sharing one origin causes no clash.

## Design

1. **New `portal/` package** (stdlib only), run with `PYTHONPATH=client/src:rotator:. python -m portal [--port 8760]`.
   - It builds a `ConsoleServer` and a `KcpServer` with the **same token** (`.console/session-token`)
     but doesn't start their HTTP servers. It sets `port`, `host` and `static_files` on each
     (the fields `start()` would set), so `allowed_hosts()` matches the portal's port.
   - It runs one `ThreadingHTTPServer` whose handler routes by path:
     - `/oidc/*`: the prefix is removed and the request goes to the serverless handler (its API and static files).
     - `/`, `/gcp*`, `/azure*`: the serverless handler (the index page).
     - anything else (`/clusters*`, `/configure`, `/api/*`, `/static/*`): the KCP handler, unchanged.
   - It uses the same CLI flags as both mains: `--no-browser`, `--new-token`, `--print-url`,
     `--fixtures`, `--kcp-fixtures`, `--local-apply`, the loopback bind check, and
     `pipelines/delivery.json` for serverless delivery.
   - Stopping it stops both consoles' run managers and simulation workers.
2. **Serverless seam** (`console/`; the default behaviour stays identical):
   - `ConsoleServer(base="")`. When `base` is set, `index.html` gets
     `<meta name="console-base" content="/oidc">` and its `/static/` links are rewritten to `/oidc/static/`.
   - `app.js`: `BASE` is read from that meta, and `api()` and `streamEvents()` fetch `BASE + path`.
   - `index.html`: a hidden "Clusters" nav group (AKS, GKE, EKS, OpenShift links to `/clusters/<p>`)
     and a "Kubernetes clusters" card on the `/` pick page, shown only when `BASE` is set.
3. **KCP seam** (`kcp_console/`, done by the KCP owner session):
   - `index.html`: a hidden "Serverless" nav group (Google Cloud `/gcp`, Azure `/azure`), shown
     when the page carries `<meta name="console-portal">` (the portal adds it, which standalone stays without).
   - `app.js` `route()`: let links outside `/clusters*` and `/configure` do a full page load
     instead of the in-app router.
   - Optional: export a public `make_handler`, so the portal doesn't import `_make_handler`.
4. **Look**: both use the same topbar, sidebar and token CSS. The brand becomes "JFrog OIDC" in
   both when they run inside the portal. Full CSS unification is out of scope for the first cut.

## Tests first

- `portal/tests/test_portal.py`: routing (each path reaches the right console), one token
  accepted by both APIs, a bad Host or Origin refused on every prefix, a token-less `/oidc/api`
  call gets 401, the index meta and `/oidc/static/` rewrites, and replay mode with both fixture sets.
- The serverless and KCP test suites still pass unchanged (standalone default).
- Redaction: the portal's log path goes through `console/redact.py` and is added to the redaction test.
- Browser check of the combined UI: switch from serverless to clusters and back while staying signed in.

## Order

1. Wait: the serverless and KCP sessions finish their current work and push.
2. Serverless seam and the portal (this session), with tests.
3. The KCP seam is handed to the KCP owner session with this file as the spec. The portal works before
   that lands; KCP pages just lack the link back to Serverless until it does.
4. CLAUDE.md commands: add `python -m portal`. Commit and push by path.
