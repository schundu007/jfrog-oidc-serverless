// Serverless (/serverless/): one list of every serverless workload in every cloud, read live, onboarded
// here or not, in the Azure portal's browse style: + Create and Refresh, "Filter for any field…" with
// Cloud, Type and Status pills, then the table. /<cloud> and /<cloud>?type=<kind> open it pre-filtered.
// A row opens its workload when onboarded, else New workload on its type.

import { $, api, h, fill, BASE, CLOUD, TYPE, template, help, status as setStatus, refreshing } from "./core.js";

const LIVE = ["azure", "gcp"];   // the clouds read live
const NAMES = { azure: "Azure", gcp: "Google Cloud" };
const ls = { rows: [], configs: [], schema: null, errors: [], loading: 0 };
const F = { q: "", cloud: LIVE.includes(CLOUD) ? CLOUD : "", type: TYPE || "", state: "" };

const iconOf = (k) => window.consoleNav?.section(k)?.icon || "run";
const iconEl = (key, cls) => {
  const s = h("span", { class: cls });
  s.innerHTML = window.consoleNav?.icon(key) || "";   // constant SVG only, never data
  return s;
};
const cloudOf = (k) => String(k).split("_")[0];
const kindLabel = (k) => ls.schema?.kinds?.[k]?.label || k;

// The saved configuration that targets this cloud workload, if any (same kind, same workload name).
const configOf = (w) => ls.configs.find((c) => c.kind === w.kind && String(c.workload || "").split(" / ").pop() === w.name);

// Filters live in the address, so a refresh or a shared link shows the same list.
function saveQuery() {
  const u = new URLSearchParams();
  for (const k of ["q", "cloud", "type", "state"]) if (F[k]) u.set(k, F[k]);
  history.replaceState(null, "", `${BASE}/${u.toString() ? `?${u}` : ""}`);
}

function matches(w) {
  if (F.cloud && w.cloud !== F.cloud) return false;
  if (F.type && w.kind !== F.type) return false;
  if (F.state && (F.state === "on") !== !!w.cfg) return false;
  const q = F.q.trim().toLowerCase();
  return !q || [w.name, w.region, kindLabel(w.kind), w.resource_group, w.project, w.subscription_name, w.subscription, NAMES[w.cloud]]
    .some((v) => String(v || "").toLowerCase().includes(q));
}

// Cloud portal links come from the server (links.resource_group, links.scope); plain text without one. New tab.
const out = (href, text) => (href ? h("a", { href, target: "_blank", rel: "noopener noreferrer", text }) : text);

function row(w) {
  const open = w.cfg ? `${BASE}/${w.cloud}/w/${encodeURIComponent(w.cfg.name)}` : `${BASE}/${w.cloud}/new?kind=${encodeURIComponent(w.kind)}`;
  const tr = h("tr", { class: "row-link", "data-name": w.name },
    h("td", {}, h("span", { class: "blade-name" }, iconEl(iconOf(w.kind), "blade-ico"), h("a", { href: open, text: w.name }))),
    h("td", {}, h("span", { class: `blade-state ${w.cfg ? "ok" : ""}`, text: w.cfg ? "Onboarded" : "Not onboarded" })),
    h("td", {}, h("a", { href: `${BASE}/${w.cloud}?type=${encodeURIComponent(w.kind)}`, text: kindLabel(w.kind) })),
    h("td", { text: w.region || "—" }),
    w.cloud === "azure" ? h("td", {}, out(w.links?.resource_group, w.resource_group || "—")) : null,
    h("td", {}, out(w.links?.scope, w.project || w.subscription_name || w.subscription || "—")));
  tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.assign(open); });
  return tr;
}

function render() {
  const kinds = Object.keys(ls.schema?.kinds || {}).filter((k) => LIVE.includes(cloudOf(k)) && (!F.cloud || cloudOf(k) === F.cloud));
  if (F.type && !kinds.includes(F.type)) F.type = "";
  const set = (k) => (v) => { F[k] = v; saveQuery(); render(); };
  fill($("wl-filters"), window.consoleBlade.bar({ q: F.q, onQuery: (v) => { F.q = v; saveQuery(); rows(); }, pills: [
    { label: "Cloud", value: F.cloud, options: LIVE.map((c) => [c, NAMES[c]]), onChange: set("cloud") },
    { label: "Type", value: F.type, options: kinds.map((k) => [k, kindLabel(k)]), onChange: set("type") },
    { label: "Status", value: F.state, options: [["on", "Onboarded"], ["off", "Not onboarded"]], onChange: set("state") },
  ] }));
  if ($("wl-create-panel")?.childElementCount) fill($("wl-create-panel"), createPanel());
  rows();
}

function rows() {
  const shown = ls.rows.filter(matches);
  // One table per cloud, each with its own columns; a cloud filtered out has no table.
  const HEADS = { azure: ["Name", "Status", "Type", "Location", "Resource group", "Subscription"],
                  gcp: ["Name", "Status", "Type", "Location", "Project"] };
  fill($("wl-tables"), LIVE.filter((c) => (!F.cloud || c === F.cloud) && shown.some((w) => w.cloud === c)).map((c) =>
    h("section", { class: "ov-group wl-cloud" },
      h("h2", { class: "ov-group-title" }, iconEl(c, "blade-ico"), h("a", { href: `${BASE}/${c}`, text: NAMES[c] })),
      h("div", { class: "table-scroll" }, h("table", { class: "blade-table wl-table" },
        h("thead", {}, h("tr", {}, HEADS[c].map((t) => h("th", { scope: "col", text: t })))),
        h("tbody", {}, shown.filter((w) => w.cloud === c).map(row)))))));
  const busy = ls.loading > 0;
  $("wl-count").textContent = busy && !ls.rows.length ? "" : `Showing ${shown.length} of ${ls.rows.length} records.`;
  $("wl-empty").hidden = busy || shown.length > 0;
  $("wl-empty").textContent = ls.rows.length ? "No workloads match the filters" : "No serverless workloads in the clouds";
}

// + Create: every service type, by cloud. Creating a workload is infrastructure, its own step: each tile
// opens the Create flow on the Infrastructure page: Name → Settings → Plan → Deploy, Back on each step.
// Onboarding OIDC is the row's link.
const STACK_OF = {
  gcp_run_service: "gcp-cloud-run-service", gcp_run_job: "gcp-cloud-run", gcp_run_function: "gcp-cloud-run-function",
  gcp_run_worker_pool: "gcp-cloud-run-worker-pool", gcp_app_engine: "gcp-app-engine", azure_container_app: "azure-container-app",
  azure_container_app_job: "azure-container-apps", azure_functions_aca: "azure-functions-aca",
  azure_functions_premium: "azure-functions-premium", azure_functions_flex: "azure-functions-flex", azure_web_app: "azure-web-app",
};
function createPanel() {
  const kinds = Object.keys(ls.schema?.kinds || {}).filter((k) => LIVE.includes(cloudOf(k)));
  return LIVE.filter((c) => !F.cloud || c === F.cloud).map((c) => h("section", { class: "ov-group" }, h("h2", { class: "ov-group-title" }, iconEl(c, "blade-ico"), NAMES[c]),
    h("div", { class: "ov-tiles" }, kinds.filter((k) => cloudOf(k) === c).map((k) =>
      h("a", { class: "ov-tile", href: `/setup?${new URLSearchParams(STACK_OF[k] ? { create: STACK_OF[k] } : { cloud: c, kind: "Serverless" })}` }, iconEl(iconOf(k), "ov-tile-icon"), h("span", { text: kindLabel(k) }))))));
}

// What exists in each cloud now (the same read-only listing the New workload picker uses), and the
// saved configurations, to mark and open the onboarded ones. One cloud at a time: they share the CLI lock.
async function load(refresh = false) {
  ls.rows = [];
  ls.errors = [];
  ls.loading = LIVE.length;
  rows();
  setStatus($("list-status"), "Reading the clouds", "busy");
  ls.configs = (await api("/api/configs")).configs;
  for (const c of LIVE) {
    try {
      const inv = await api(`/api/inventory/${c}${refresh ? "?refresh=1" : ""}`);
      const known = (k) => Object.hasOwn(ls.schema?.kinds || {}, k);
      for (const w of inv.workloads || []) {
        if (w.supported && known(w.kind)) ls.rows.push({ ...w, cloud: c, subscription_name: w.subscription_name || inv.defaults?.subscription_name, cfg: null });
      }
      for (const e of inv.errors || []) ls.errors.push(`${NAMES[c]}: ${e}`);
    } catch (e) {
      ls.errors.push(`${NAMES[c]}: ${e.message}`);
    }
    ls.loading -= 1;
    for (const w of ls.rows) w.cfg = configOf(w) || null;
    rows();
  }
  setStatus($("list-status"), ls.errors.length ? `Could not read part of the clouds` : "", ls.errors.length ? "error" : "");
  fill($("wl-help"), ls.errors.length ? help(ls.errors.join(" ")) : null);
}

export async function mountList(page, { schema }) {
  ls.schema = schema;
  const u = new URLSearchParams(location.search);
  Object.assign(F, { q: u.get("q") || "", state: ["on", "off"].includes(u.get("state")) ? u.get("state") : "" });
  if (!F.type && schema.kinds?.[u.get("type")]) F.type = u.get("type");
  if (!F.cloud && LIVE.includes(u.get("cloud"))) F.cloud = u.get("cloud");
  page.replaceChildren(template("tpl-list"));
  $("wl-create").addEventListener("click", () => {
    const p = $("wl-create-panel");
    p.hidden = !p.hidden;
    $("wl-create").setAttribute("aria-expanded", String(!p.hidden));
    if (!p.hidden && !p.childElementCount) fill(p, createPanel());
    if (!p.hidden) p.scrollIntoView({ block: "nearest" });
  });
  refreshing($("wl-refresh"), () => load(true).catch((e) => { setStatus($("list-status"), e.message, "error"); throw e; }));
  // The type tiles stay folded until + Create opens them, above the list, as in the Azure portal.
  saveQuery();
  render();
  try {
    await load();
  } catch (e) {
    setStatus($("list-status"), `Could not list workloads: ${e.message}`, "error");
  }
}
