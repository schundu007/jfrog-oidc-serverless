// Configure: one cluster's settings. Validation is live (POST /api/values); Save goes back to Clusters,
// where Set up runs. Nothing here changes the cluster or JFrog.

import { api, h, fill, state, refreshConfigs, selectTab } from "./app.js";
import { $ } from "./ui.js";

const P = () => $("panel-configure");
let cfg = null;
let blank = null;

const F = {
  "cluster.name": ["Cluster", "aks-prod-eastus-01"],
  "cluster.project_id": ["Project ID", "my-gcp-project"],
  "cluster.location": ["Location", "us-central1"],
  "cluster.google_service_account": ["Google service account", "blank: jfrog-kcp@<project>.iam.gserviceaccount.com"],
  "cluster.subscription_id": ["Subscription ID", "00000000-0000-0000-0000-000000000000"],
  "cluster.resource_group": ["Resource group", "rg-aks-prod"],
  "jfrog.host": ["JFrog host", "yourcompany.jfrog.io"],
  "jfrog.test_repo": ["Docker repository", "docker-remote"],
  "jfrog.test_image": ["Test image", "yourcompany.jfrog.io/docker-remote/library/busybox:1.36"],
  "jfrog.mapped_user": ["Mapped JFrog user", "pull-user"],
  "jfrog.oidc_provider": ["OIDC provider", "aks-prod-oidc"],
  "jfrog.mapping_name": ["Identity mapping", "aks-prod-oidc-mapping"],
  "k8s.namespace": ["Namespace", "jfrog"],
  "k8s.service_account": ["Service account", "jfrog-provider-sa"],
  "k8s.release": ["Helm release", "jfrog-cred-provider"],
  "values.chart_version": ["Chart version", "1.4.0"],
  "values.default_cache_duration": ["Token cache", "30m"],
};
const CLUSTER = {
  aks: ["cluster.name", "cluster.subscription_id", "cluster.resource_group"],
  gke: ["cluster.name", "cluster.project_id", "cluster.location", "cluster.google_service_account"],
};
const GROUPS = [
  ["Cluster", null],
  ["JFrog", ["jfrog.host", "jfrog.test_repo", "jfrog.test_image", "jfrog.mapped_user", "jfrog.oidc_provider", "jfrog.mapping_name"]],
  ["Credential provider", ["k8s.namespace", "k8s.service_account", "k8s.release", "values.chart_version", "values.default_cache_duration"]],
];
const ADVANCED = ["values.match_images", "values.download_url", "values.azure_app_audience", "values.node_affinity_label",
  "jfrog.scope", "jfrog.audience", "jfrog.expires_in", "jfrog.protected_providers", "k8s.test_pod", "cluster.nodes"];

export function initConfigure() {
  state.on("config", load);
  state.on("new-config", () => { cfg = structuredClone(blank); selectTab("configure"); render(); });
}

async function load(name) {
  blank ||= await api("/api/defaults");
  cfg = structuredClone(blank);
  if (name) {
    try { cfg = (await api(`/api/configs/${encodeURIComponent(name)}`)).config; } catch { /* new */ }
  }
  render();
}

const get = (path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), cfg);
function set(path, val) {
  const ks = path.split(".");
  const last = ks.pop();
  ks.reduce((o, k) => (o[k] ??= {}), cfg)[last] = val;
}

// The values file follows the JFrog fields: the registry is the JFrog host (images are
// <host>/<repo>/<image>), and the provider in the values file is the one Set up creates.
function derive() {
  const j = cfg.jfrog, v = cfg.values;
  j.host = (j.host || "").replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  v.artifactory_url = j.host;
  v.provider_name = j.oidc_provider;
  if (j.host && !(v.match_images || []).length) v.match_images = [j.host];
  if (j.host) j.test_repo_host = j.host;
  if (j.oidc_provider && !j.mapping_name) j.mapping_name = `${j.oidc_provider}-mapping`;
}

function field(path, label, placeholder) {
  const v = get(path);
  const id = `f-${path.replace(/\./g, "-")}`;
  const onchange = (e) => {
    let val = e.target.type === "checkbox" ? e.target.checked : e.target.value.trim();
    if (Array.isArray(v)) val = val.split(",").map((x) => x.trim()).filter(Boolean);
    else if (typeof v === "number") val = Number(val);
    set(path, val);
    derive();
    validateSoon();
  };
  const input = typeof v === "boolean" ? h("input", { id, type: "checkbox", checked: v, onchange })
    : h("input", { id, type: "text", value: Array.isArray(v) ? v.join(", ") : v ?? "", placeholder: placeholder || "", spellcheck: "false", autocomplete: "off", onchange });
  return h("label", { class: "tfield", for: id }, h("span", {}, label || path.split(".").pop().replace(/_/g, " ")), input,
    h("span", { class: "issue small", "data-field": path }));
}

function render() {
  derive();
  const saved = cfg.name && cfg.name === state.config;
  fill(P(),
    h("div", { class: "cfg-head" }, h("div", {}, h("h1", {}, saved ? cfg.name : "New cluster")),
      h("div", { class: "head-actions" },
        saved ? h("button", { class: "link danger", onclick: remove }, "Delete") : null,
        h("button", { class: "secondary", onclick: () => back(saved ? cfg.name : null) }, "Cancel"),
        h("button", { class: "primary", onclick: async () => { if (await save()) back(cfg.name); } }, "Save"))),
    h("p", { class: "small", id: "cfg-msg" }),
    h("div", { class: "cfg-form" },
      h("div", { class: "tgrid" }, field("name", "Name", "aks-prod-eastus-01")),
      ...GROUPS.map(([title, paths]) => [title, paths || CLUSTER[cfg.platform || "aks"] || CLUSTER.aks]).map(([title, paths]) => h("section", { class: "block" }, h("h2", {}, title),
        h("div", { class: "tgrid" }, paths.map((p) => field(p, ...F[p]))))),
      h("details", { class: "advanced" }, h("summary", {}, "Advanced"),
        h("div", { class: "tgrid" }, ADVANCED.map((p) => field(p))))));
  validateSoon(0);
}

const filled = (path) => { const v = get(path); return Array.isArray(v) ? v.length > 0 : v !== "" && v != null; };

let timer = null;
function validateSoon(ms = 300) {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    let v;
    try { v = await api("/api/values", { method: "POST", body: { config: cfg } }); } catch { return; }
    document.querySelectorAll("#panel-configure .issue").forEach((el) => { el.textContent = ""; el.className = "issue small"; });
    for (const i of v.issues.filter((x) => x.level === "error" && filled(x.field))) {
      const el = document.querySelector(`#panel-configure .issue[data-field="${i.field}"]`);
      if (el && !el.textContent) { el.textContent = i.message.split(/[:(]/)[0].trim(); el.title = i.message; el.classList.add(i.level); }
    }
  }, ms);
}

// Back to the cluster's own page (or the fleet).
function back(name) {
  history.pushState({}, "", name ? `/clusters/${encodeURIComponent(name)}` : "/clusters");
  selectTab("overview", { history: "none" });
}

function msg(text, cls = "small") { const m = $("cfg-msg"); if (m) { m.textContent = text; m.className = cls; } }

async function save() {
  const name = (cfg.name || "").trim();
  if (!name) { msg("Name required", "small error"); return false; }
  try {
    await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: cfg } });
    await refreshConfigs(name);
    state.emit("config", name);
    return true;
  } catch (e) { msg(e.message, "small error"); return false; }
}

async function remove() {
  const name = state.config;
  const m = $("cfg-msg");
  if (!name || !m) return;
  if (m.dataset.armed !== name) { m.dataset.armed = name; msg(`Click Delete again to delete ${name}.`, "small error"); return; }
  await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
  await refreshConfigs(null);
  state.emit("config", state.config);
  back(null);
}
