// Clusters: the saved clusters with their state, the selected cluster's direct actions, and the
// AKS clusters the operator's az login can see, each one a click away from a configuration.

import { api, h, fill, state, selectTab, refreshConfigs } from "./app.js";
import { $, pill, pageHead, stepList } from "./ui.js";

let aks = null;           // {clusters, error} from GET /api/aks, loaded once per page view

export function initOverview() {
  state.on("config", render);
  state.on("runs", render);
  state.on("status", render);
  render();
}

const real = (r) => r.request.parameters.DryRun !== true;
const stepOf = (r, id) => ((r.result || {}).steps || []).find((s) => s.id === id);
const onOff = (b) => h("span", { class: `pill ${b ? "pass" : "fail"}` }, b ? "on" : "off");

async function render() {
  const P = $("panel-overview");
  const configs = state.configs || [];
  let runs = [];
  try { runs = (await api("/api/runs")).runs; } catch { /* none */ }
  if (!aks) loadAks();
  const rows = configs.map((c) => {
    const rs = runs.filter((r) => r.config === c.name && real(r));
    const setup = rs.find((r) => (r.kind === "direct" && r.name === "setup") || r.name === "INSTALL_JFROG_PROVIDER");
    const pull = rs.find((r) => (r.kind === "direct" && stepOf(r, "pull")) || r.name === "CREATE_TEST_POD");
    const pullStatus = pull && (pull.kind === "direct" ? (stepOf(pull, "pull").status === "done" ? "succeeded" : "failed") : pull.status);
    return h("tr", { class: c.name === state.config ? "is-open" : "", onclick: () => select(c.name) },
      h("td", {}, h("b", {}, c.name)),
      h("td", {}, c.cluster || h("span", { class: "muted" }, "—")),
      h("td", {}, setup ? pill(setup.status) : h("span", { class: "muted" }, "—")),
      h("td", {}, pull ? pill(pullStatus) : h("span", { class: "muted" }, "—")),
      h("td", { class: "muted small" }, (c.updated || "").slice(0, 16).replace("T", " ")),
      h("td", { class: "row-actions" },
        h("button", { class: "secondary small-btn", onclick: (e) => { e.stopPropagation(); open(c.name); } }, "Edit")));
  });
  const cur = configs.find((c) => c.name === state.config);
  fill(P,
    pageHead("Clusters", h("button", { class: "primary", onclick: () => state.emit("new-config") }, "New cluster")),
    h("table", { class: "cfg-table clickable" },
      h("thead", {}, h("tr", {}, ["Name", "AKS cluster", "Provider", "Pull test", "Updated", ""].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: "6", class: "muted" }, "None yet")))),
    cur ? await selected(cur) : null,
    aksTable());
}

async function selected(c) {
  return h("section", { class: "summary" },
    h("h2", {}, c.name, h("span", { class: "muted" }, ` · ${c.cluster || "—"}`)),
    await stepList([
      { title: "Set up", kind: "direct", name: "setup", dryRun: true, line: "OIDC issuer, service account, JFrog provider and mapping, node label, helm install, pull test" },
      { title: "Verify", kind: "direct", name: "verify", dryRun: true, line: "DaemonSet ready and a pull with imagePullSecrets: []" },
      { title: "Tear down", kind: "direct", name: "teardown", dryRun: true, line: "helm uninstall, unlabel, delete mapping and provider" },
    ]));
}

function aksTable() {
  const head = h("div", { class: "cfg-head" }, h("h2", {}, "AKS clusters"),
    h("button", { class: "secondary", onclick: () => { aks = null; render(); } }, "Refresh"));
  if (!aks || aks.loading) return h("div", {}, head, h("p", { class: "muted" }, "Loading…"));
  if (aks.error) return h("div", {}, head, h("p", { class: "error small" }, aks.error));
  return h("div", {}, head, h("table", { class: "cfg-table" },
    h("thead", {}, h("tr", {}, ["Cluster", "Resource group", "Location", "Kubernetes", "OIDC issuer", "Workload Identity", ""].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, aks.clusters.length ? aks.clusters.map((k) => h("tr", {},
      h("td", {}, h("b", {}, k.name), k.power && k.power !== "Running" ? h("span", { class: "muted small" }, ` ${k.power}`) : null),
      h("td", {}, k.resource_group), h("td", {}, k.location), h("td", {}, k.version),
      h("td", {}, onOff(!!k.oidc_issuer)), h("td", {}, onOff(k.workload_identity)),
      h("td", { class: "row-actions" }, k.configured ? h("span", { class: "muted small" }, "added")
        : h("button", { class: "primary small-btn", onclick: () => add(k) }, "Add"))))
      : h("tr", {}, h("td", { colspan: "7", class: "muted" }, "None found")))));
}

async function loadAks() {
  aks = { clusters: [], loading: true };
  try { aks = await api("/api/aks"); } catch (err) { aks = { clusters: [], error: err.message }; }
  render();
}

// A new configuration from a listed cluster: the cluster fields filled, the rest from the defaults.
async function add(k) {
  const cfg = await api("/api/defaults");
  const name = k.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 63);
  cfg.name = name;
  Object.assign(cfg.cluster, { name: k.name, resource_group: k.resource_group, subscription_id: k.subscription_id, oidc_issuer: k.oidc_issuer });
  await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: cfg } });
  aks = null;
  await open(name);
}

async function select(name) {
  await refreshConfigs(name);
  state.emit("config", name);
}

async function open(name) {
  await select(name);
  selectTab("configure");
}
