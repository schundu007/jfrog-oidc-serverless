# Console page pattern (Serverless and Kubernetes, one tool)

Modelled on Azure DevOps Pipelines: a list page, a detail page with tabs, a run page with a step tree next to the log. Both consoles (`console/static`, `kcp_console/static`) follow this file; the portal shell (top bar, sidebar, breadcrumb) stays as it is.

## Pages

| ADO | Serverless | Kubernetes |
|---|---|---|
| Pipelines (list) | `/<cloud>`: Workloads | `/clusters/<cloud>`: Clusters |
| Pipeline (detail, tabs) | `/<cloud>/w/<name>`: Runs · Configuration · Simulator | `/clusters/<cloud>/<name>`: Summary · Runs · Configuration |
| New pipeline | `/<cloud>/new`: Configuration only; Save creates it and opens its page | Add a cluster (as now) |
| Run (step tree + log) | `/<cloud>/w/<name>/runs/<id>` | `/clusters/<cloud>/<name>/runs/<id>` |

Old paths (`/<cloud>/configure`, `/runs`, `/simulate`) land on the list. The breadcrumb walks up the levels.

## List page
- Header: title (h1 over the green bar), a count, the primary action on the right ("New workload", "Add a cluster").
- One table. First column: status icon of the last run. Then name (link), type, scope, **Last run** ("succeeded · 2 h ago · 1 m 32 s"). The whole row opens the detail page. Row actions in a trailing "⋯" menu (Delete).
- A filter box over the table. The Kubernetes KPI tiles stay.

## Detail page
- Header: name (h1), status pill of the last run, key facts on one muted line (type · scope · identity). Actions on the right: primary **Run pipeline** / **Verify**, secondary **Preflight** / **Set up again**, and a "⋯" menu for Delete, Clear old runs, Tear down. Destructive actions live in the menu, never as plain buttons, and confirm with a second click.
- Text tabs under the header with a green underline on the active one. The URL carries the tab (`?tab=runs|config|sim|summary`). Default: Runs when there are runs (Summary on Kubernetes), otherwise Configuration.
- **Approval banner**: when a run waits for approval, a banner under the header on every tab: "1 approval needs review" + Review.
- **Runs tab**: this item's runs, newest first: status icon, run ID (link to the run page), started, duration, a pip strip (one small segment per step, coloured by status). Toolbar: Refresh, Clear finished; per-row "⋯": Delete run.
- **Configuration tab**: the form. Step cards always open (ADO settings pages do not hide fields). Save / Preflight in a bar at the top of the tab. Preflight results open in a right-hand side panel (flyout) with a close button.
- **Simulator tab** (Serverless): one sentence on what it does (Run pipeline already simulates before it changes anything; this is for rehearsing faults by hand), the scenario form, then results.

## Run page
- Header: run ID, status pill, started · duration, link back to the workload/cluster. Actions: Cancel (active), Delete (finished).
- Two columns: left a step tree with a status icon and duration per row (selected row highlighted, keyboard up/down); right the selected step's detail and its log (line numbers, monospace, Copy).
- A waiting approval shows as a card at the top of the right pane (hash, typed confirmation).

## Status icons (one set everywhere)
Filled circle + glyph: succeeded green check, failed dark-grey cross, running green spinner ring, waiting grey clock, cancelled grey slash, skipped grey dash, not run hollow ring. Humana palette: green for good, greys for everything else.

## Collapse rules
Only secondary detail collapses: Advanced simulator settings, long evidence blocks, raw logs inside a step. Primary content (forms, tables, the run tree) never collapses. Nothing remembers open state across reloads.
