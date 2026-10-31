// Clusters: one platform's clusters at /clusters/<cloud>. KPI tiles filter the table, the search box
// narrows it, the whole row opens the cluster, and the row's "⋯" menu holds the destructive actions.
// Add a cluster lists the clusters the operator's cloud logins can see and saves one as a new configuration.

import { api, h, fill, state, refreshConfigs, route, go, url, clusterPath, runPath, markOf, CLOUDS as CLOUD } from "./app.js";
import { $, muted, brief, icon, ago, duration, moreMenu, link, TITLES } from "./ui.js";

const PAGE = 25;
const STATE = {
  approval: ["warn", "Awaiting approval"], running: ["info", "Running…"], attention: ["fail", "Needs attention"], not_proven: ["fail", "OIDC not proven"],
  unverified: ["warn", "Not verified"], working: ["ok", "OIDC working"], not_installed: ["muted", "Not set up"], unread: ["muted", "Not read"],
  missing: ["fail", "Missing"],
};
const STATS = [["", "All clusters"], ["working", "OIDC working"], ["attention", "Needs attention"], ["not_proven", "Not proven"],
  ["approval", "Awaiting approval"], ["not_installed", "Not set up"]];
const STAT_HELP = {
  "": "Every cluster on this platform.",
  working: "A pull through JFrog was proven with no pull secret.",
  attention: "A check fails: the provider, the JFrog side or the values file.",
  not_proven: "Installed, but no pull through JFrog has been proven yet.",
  approval: "A dry run passed and waits for your approval.",
  not_installed: "The credential provider isn't installed yet.",
};
const RUN_TEXT = { prepared: "waiting for approval", queued: "queued", running: "running", succeeded: "succeeded", failed: "failed",
  cancelled: "cancelled", interrupted: "interrupted", refused: "refused", error: "failed" };

const F = { q: "", state: "", page: 1 };
let fleet = null;
let fleetLoading = false;
let adding = false;

const cloud = () => route().cloud || "aks";
const P = () => $("panel-list");

export function initClusters() {
  state.on("page", (r) => {
    if (r.page !== "list") return;
    readQuery();
    fleet = null;
    render();
  });
}

function render() {
  if (route().page === "list") fill(P(), view());
}

function readQuery() {
  const u = new URLSearchParams(location.search);
  Object.assign(F, { q: u.get("q") || "", state: u.get("state") || "", page: Number(u.get("page")) || 1 });
}

function query() {
  const u = new URLSearchParams();
  for (const k of ["q", "state"]) if (F[k]) u.set(k, F[k]);
  if (F.page > 1) u.set("page", F.page);
  return u.toString() ? `?${u}` : "";
}

// Filters live in the address, so a refresh or a shared link shows the same list.
function setFilter(patch) {
  Object.assign(F, { page: 1 }, patch);
  history.replaceState({}, "", url(`/clusters/${cloud()}${query()}`));
}

async function loadFleet() {
  if (fleetLoading) return;
  fleetLoading = true;
  try { fleet = await api("/api/fleet"); } catch (err) { fleet = { clusters: [], counts: {}, error: err.message }; }
  fleetLoading = false;
  render();
  // What the cloud says now: a cluster that no longer exists is Missing, whatever its last run proved.
  const shown = fleet;
  try {
    const { clusters } = await api("/api/fleet/live");
    for (const r of shown.clusters) if (clusters[r.name] === "missing") r.state = "missing";
    if (fleet === shown) render();
  } catch { /* the last-read states stay */ }
}

function matches(r) {
  if (F.state && r.state !== F.state) return false;
  const q = F.q.trim().toLowerCase();
  return !q || [r.name, r.cluster, r.location, r.subscription, r.resource_group].some((v) => (v || "").toLowerCase().includes(q));
}

function view() {
  if (!fleet && !fleetLoading) loadFleet();
  const d = fleet || { clusters: [], platforms: {} };
  const k = cloud(), label = CLOUD[k];
  const built = (d.platforms || {})[k] !== false && !!SOURCES[k];
  const mine = d.clusters.filter((r) => (r.cloud || "aks") === k);
  const counts = {};
  for (const r of mine) counts[r.state] = (counts[r.state] || 0) + 1;
  const n = mine.length;
  const open = built && fleet && (adding || !n);   // no clusters yet: the list to add from is the page
  const where = k === "gke" ? "project or location" : "subscription, resource group or region";
  return h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", { class: "page-id" }, h("div", { class: "page-title" }, h("span", { class: "head-mark" }, markOf(k)), h("h1", {}, label),
        n ? muted(`${n} cluster${n === 1 ? "" : "s"}`) : null)),
      h("div", { class: "head-actions" },
        built && n ? h("button", { class: adding ? "secondary" : "primary", onclick: () => { adding = !adding; render(); } }, adding ? "Close" : "Add a cluster") : null,
        built ? h("button", { class: "ghost", disabled: fleetLoading, onclick: () => { fleet = null; render(); } }, "Refresh") : null)),
    d.error ? h("div", { class: "callout fail" }, brief(d.error)) : null,
    !built ? notYet(k, label) : null,
    open ? addPanel(state.configs || [], k) : null,
    !built ? null
      : !fleet ? muted("Loading…")
      : n ? [
        h("div", { class: "stats" }, STATS.map(([s, text]) => h("button", {
          class: `stat${F.state === s ? " on" : ""}${s ? ` s-${s}` : ""}`, "aria-pressed": F.state === s ? "true" : "false",
          onclick: () => { setFilter({ state: F.state === s ? "" : s }); render(); },
          "data-tip": STAT_HELP[s], "aria-description": STAT_HELP[s],
        }, h("b", {}, String(s ? counts[s] || 0 : n)), h("span", {}, text)))),
        h("div", { class: "filter-bar" },
          h("input", { type: "search", class: "search", placeholder: `Filter by name, ${where}`, value: F.q, "aria-label": "Filter clusters",
            oninput: (e) => { setFilter({ q: e.target.value }); fillTable(); } })),
        h("div", { id: "fleet-table" }, table(mine)),
      ] : null);
}

function fillTable() {
  const el = $("fleet-table");
  if (el && fleet) fill(el, table(fleet.clusters.filter((r) => (r.cloud || "aks") === cloud())));
}

// "Verify · failed · 22 min ago · 1 m 32 s"
function lastRun(a) {
  if (!a) return muted("No runs yet");
  const took = a.started && a.finished ? duration(a.started, a.finished) : "";
  return [h("div", {}, `${TITLES[a.which] || a.which}${a.dry ? " · dry run" : ""}`),
    h("div", { class: "sub" }, [RUN_TEXT[a.status] || a.status, ago(a.at), took].filter(Boolean).join(" · "))];
}

function table(all) {
  const rows = all.filter(matches);
  const gke = cloud() === "gke";
  if (!rows.length) return h("div", { class: "empty slim" }, h("p", {}, "No clusters match"),
    h("button", { class: "ghost", onclick: () => { setFilter({ q: "", state: "" }); render(); } }, "Clear filters"));
  const pages = Math.ceil(rows.length / PAGE);
  F.page = Math.min(Math.max(1, F.page), pages);
  const first = (F.page - 1) * PAGE;
  const turn = (p) => { F.page = p; history.replaceState({}, "", url(`/clusters/${cloud()}${query()}`)); fillTable(); };
  const heads = ["", "Cluster", gke ? "Project" : "Subscription", gke ? null : "Resource group", "Status", "Credential provider", "Last run", ""];
  return [
    h("table", { class: "table fleet-table" },
      h("thead", {}, h("tr", {}, heads.filter((t) => t !== null).map((t, i) => h("th", { scope: "col", class: i === 0 ? "c-ico" : null,
        "aria-label": i === 0 ? "Last run status" : t === "" ? "Actions" : null }, t)))),
      h("tbody", {}, rows.slice(first, first + PAGE).map((r) => {
        const [cls, text] = STATE[r.state] || ["muted", r.state];
        const a = r.action;
        const open = () => go(clusterPath(r.name));
        return h("tr", { tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) open(); } },
          h("td", { class: "c-ico" }, a ? icon(a.status) : icon("notrun", "No runs")),
          h("td", {}, h("div", { class: "nm" }, link(go, url, clusterPath(r.name), r.name)),
            h("div", { class: "sub" }, [r.cluster !== r.name ? r.cluster : "", r.location].filter(Boolean).join(" · "))),
          h("td", { class: "wrap" }, r.subscription || muted("—")),
          gke ? null : h("td", { class: "wrap" }, r.resource_group || muted("—")),
          h("td", {}, h("span", { class: `badge ${cls}` }, text), r.problem ? h("div", { class: "sub" }, brief(r.problem)) : null),
          h("td", {}, r.chart ? h("div", {}, `v${r.chart}`) : muted("—"), r.ready ? h("div", { class: "sub" }, `${r.ready} nodes ready`) : null),
          h("td", {}, a?.id ? link(go, url, runPath(r.name, a.id), lastRun(a)) : lastRun(a)),
          h("td", { class: "c-more", onclick: (e) => e.stopPropagation() }, moreMenu([
            { label: "Open", onclick: open },
            { label: "Edit configuration", onclick: () => go(clusterPath(r.name, "config")) },
            { label: "Clear old runs", danger: true, onclick: () => clearRuns(r.name) },
            { label: "Remove from console", danger: true, onclick: () => remove(r.name) },
          ], `Actions for ${r.name}`)));
      }))),
    pages > 1 ? h("div", { class: "pager" },
      muted(`${first + 1}–${Math.min(first + PAGE, rows.length)} of ${rows.length}`),
      h("button", { class: "ghost", disabled: F.page <= 1, onclick: () => turn(F.page - 1) }, "Previous"),
      h("button", { class: "ghost", disabled: F.page >= pages, onclick: () => turn(F.page + 1) }, "Next")) : null,
  ];
}

async function clearRuns(name) {
  try { await api(`/api/runs?config=${encodeURIComponent(name)}`, { method: "DELETE" }); } catch { /* the row keeps its last run */ }
  fleet = null;
  render();
}

// Removes the configuration from the console only: nothing on the cluster or in JFrog changes.
async function remove(name) {
  await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
  await refreshConfigs(null);
  fleet = null;
  render();
}

// ----------------------------------------------------------------------- add a cluster

// Existing clusters the operator's logins can see, per cloud: every subscription (az) or project (gcloud).
const SOURCES = { aks: { api: "/api/aks", short: "AKS" }, gke: { api: "/api/gke", short: "GKE" } };
const found = {};           // cloud -> {clusters, loading, error}

function addPanel(configs, k) {
  return h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, `Add a ${SOURCES[k].short} cluster`)), foundTable(configs, k));
}

function foundTable(configs, k) {
  const src = SOURCES[k], f = found[k];
  const added = new Set(configs.map((c) => (c.cluster || "").toLowerCase()));
  if (!f) { loadFound(k); return muted(`Finding ${src.short} clusters…`); }
  if (f.loading) return muted(`Finding ${src.short} clusters…`);
  if (f.error) return h("p", { class: "error small" }, brief(f.error));
  const rows = f.clusters.filter((c) => !added.has(c.name.toLowerCase()));
  if (!rows.length) return muted(f.clusters.length ? `All ${src.short} clusters are added.` : `No ${src.short} clusters found.`);
  // One card per cluster: its mark, name, where it lives, what Set up will find, then Add.
  const fact = (ok, text) => h("li", { class: ok ? "" : "bad" }, `${text} ${ok ? "on" : "off"}`);
  return h("div", { class: "pick-grid" }, rows.map((c) => {
    const where = k === "gke" ? [c.project_name || c.project_id, c.location] : [c.subscription_name || c.subscription_id, c.resource_group, c.location];
    const st = (c.power && c.power !== "Running" && c.power) || (c.status && c.status !== "RUNNING" && c.status.toLowerCase());
    return h("button", { type: "button", class: "pick-card", disabled: c.autopilot, onclick: () => add(c, k) },
      h("span", { class: "pick-mark" }, markOf(k)),
      h("b", {}, c.name),
      h("span", { class: "pick-sub" }, where.filter(Boolean).join(" · ")),
      h("ul", { class: "pick-list" },
        h("li", {}, `Kubernetes ${c.version || "—"}`),
        k === "gke" ? null : fact(c.oidc_issuer, "OIDC issuer"),
        c.autopilot ? h("li", { class: "bad" }, "Autopilot: not supported") : fact(c.workload_identity, "Workload Identity"),
        st ? h("li", { class: "bad" }, st) : null),
      h("span", { class: "pick-go" }, c.autopilot ? "Not supported" : "Add", c.autopilot ? null : markOf("go")));
  }));
}

async function loadFound(k) {
  found[k] = { clusters: [], loading: true };
  try { found[k] = await api(SOURCES[k].api); } catch (err) { found[k] = { clusters: [], error: err.message }; }
  render();
}

// A new configuration for an existing cluster: its coordinates from the cloud, the JFrog side copied from
// the newest saved cluster (same JFrog instance, user and test image), the provider and mapping named after
// it. Set up then adopts what the cluster and JFrog already have and creates only what is missing.
async function add(c, k) {
  const cfg = await api("/api/defaults");
  const name = c.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 63);
  cfg.name = name;
  cfg.platform = k;
  if (k === "gke") {
    Object.assign(cfg.cluster, { name: c.name, project_id: c.project_id, location: c.location, subscription_name: c.project_name || c.project_id,
      resource_group: "", subscription_id: "", google_service_account: "", oidc_issuer: "" });   // the server derives GKE's issuer
  } else {
    Object.assign(cfg.cluster, { name: c.name, resource_group: c.resource_group, subscription_id: c.subscription_id,
      subscription_name: c.subscription_name || "", oidc_issuer: c.oidc_issuer });
  }
  const newest = [...(state.configs || [])].sort((a, b) => (b.updated || "").localeCompare(a.updated || ""))[0];
  if (newest) {
    const src = await api(`/api/configs/${encodeURIComponent(newest.name)}`);
    const o = src.config || src;
    for (const f of ["host", "mapped_user", "test_repo", "test_repo_host", "test_image", "scope", "audience", "expires_in"]) cfg.jfrog[f] = o.jfrog[f];
    for (const f of ["artifactory_url", "match_images", "chart_version", "binary_version", "default_cache_duration"]) cfg.values[f] = o.values[f];
  }
  cfg.jfrog.oidc_provider = cfg.values.provider_name = `${name}-oidc`.slice(0, 63);
  cfg.jfrog.mapping_name = `${name}-mapping`.slice(0, 63);
  await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: cfg } });
  delete found[k];
  adding = false;
  fleet = null;
  await refreshConfigs(name);
  go(clusterPath(name, cfg.jfrog.host ? "summary" : "config"));
}

// A platform the console can't read yet: what it will do there, what it needs, and where to go now.
const NOT_YET = {
  eks: {
    will: "List your EKS clusters, trust each cluster's OIDC issuer in JFrog, install the JFrog credential provider on the nodes and prove a pull with no pull secret: the same Set up and Verify as AKS and GKE.",
    needs: ["The AWS CLI signed in to the account (aws sts get-caller-identity works)",
      "kubectl access to the cluster (aws eks update-kubeconfig)",
      "The cluster's OIDC issuer, which EKS turns on for every cluster",
      "A Kubernetes version whose kubelet hands service account tokens to credential providers"],
  },
  openshift: {
    will: "Read your OpenShift clusters, trust each cluster's service account issuer in JFrog, roll the JFrog credential provider out to the nodes and prove a pull with no pull secret.",
    needs: ["oc logged in to the cluster with cluster-admin (the provider is a node change)",
      "The cluster's service account issuer published where JFrog can reach it (OIDC discovery)",
      "A machine config pool the console may update, since nodes restart to pick the provider up"],
  },
};

function notYet(k, label) {
  const info = NOT_YET[k] || { will: "", needs: [] };
  const ready = Object.entries(CLOUD).filter(([c]) => !NOT_YET[c] && c !== k);
  return h("section", { class: "card not-yet" },
    h("div", { class: "card-head" }, h("h2", {}, `${label} isn't available yet`)),
    info.will ? h("p", {}, info.will) : null,
    info.needs.length ? [h("h3", {}, "What it will need"), h("ul", {}, info.needs.map((n) => h("li", {}, n)))] : null,
    h("h3", {}, "Available now"),
    h("p", { class: "ready-links" }, ready.map(([c, name]) => link(go, url, `/clusters/${c}`, markOf(c), name))));
}
