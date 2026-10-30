// Configure: one cluster's set-up as numbered steps (cluster, JFrog, credential provider,
// pipelines), with the OIDC trust chain, the planned changes and the actions beside it.
// Validation is live (POST /api/values); nothing here changes the cluster or JFrog.

import { api, h, fill, rich, state, refreshConfigs, selectTab } from "./app.js";
import { $, doc, section, table, pill } from "./ui.js";

const P = () => $("panel-configure");
let cfg = null;
let blank = null;
let lastPreflight = null;
let lastValues = null;

// Field labels, placeholders and hints, in the order the steps show them.
const F = {
  "cluster.name": ["AKS cluster name", "aks-prod-eastus-01"],
  "cluster.subscription_id": ["Subscription ID", "00000000-0000-0000-0000-000000000000"],
  "cluster.resource_group": ["Resource group", "rg-aks-prod"],
  "cluster.environment": ["Environment", ""],
  "cluster.oidc_issuer": ["OIDC issuer URL", "Discover fills this"],
  "jfrog.host": ["JFrog platform host", "yourcompany.jfrog.io"],
  "jfrog.oidc_provider": ["OIDC provider name", "aks-prod-oidc"],
  "jfrog.mapped_user": ["Mapped JFrog user", "pull-user"],
  "jfrog.test_repo": ["Docker repository key", "team-docker-local"],
  "k8s.namespace": ["Namespace", "jfrog"],
  "k8s.service_account": ["Service account", "jfrog-provider-sa"],
  "k8s.release": ["Helm release", "jfrog-cred-provider"],
  "values.default_cache_duration": ["Token cache duration", "30m"],
  "values.chart_version": ["Chart version", "1.4.0"],
  "values.download_url": ["Plugin binary URL", "chart default"],
  "ado.organization": ["Azure DevOps organization", "dev.azure.com/your-org"],
  "ado.project": ["Project", "Platform"],
  "ado.repo": ["Helm/ArgoCD repo", "platform-helm-templates"],
  "ado.branch": ["Branch", "main"],
  "ado.pilot_pipeline": ["Utility pipeline (scenarios)", "argocd-utility"],
  "ado.key_vault": ["Key Vault (download token)", "kv-ci-secrets"],
  "ado.key_vault_secret": ["Key Vault secret name", "artifactory-download-token"],
};
const STEPS = [
  ["Cluster", ["cluster.name", "cluster.subscription_id", "cluster.resource_group", "cluster.environment", "cluster.oidc_issuer"]],
  ["JFrog", ["jfrog.host", "jfrog.oidc_provider", "jfrog.mapped_user", "jfrog.test_repo"]],
  ["Credential provider", ["k8s.namespace", "k8s.service_account", "k8s.release", "values.default_cache_duration", "values.chart_version", "values.download_url"]],
];
const PIPELINE_FIELDS = ["ado.organization", "ado.project", "ado.repo", "ado.branch", "ado.pilot_pipeline", "ado.key_vault", "ado.key_vault_secret"];
const SHOWN = new Set(STEPS.flatMap((s) => s[1]).concat(PIPELINE_FIELDS));
const ADVANCED = {
  cluster: ["subscription_name", "nodes"],
  k8s: ["daemonset", "test_pod", "stale_pods", "node_label"],
  jfrog: ["mapping_name", "scope", "audience", "expires_in", "test_repo_host", "test_image", "binary_repo", "promote_source_repo", "protected_providers"],
  values: ["artifactory_url", "match_images", "provider_name", "azure_app_audience", "token_attributes_enabled", "chart_repo", "chart_name", "binary_version", "rbac_create", "node_affinity_label"],
  ado: ["cluster_pipeline", "mappings_pipeline", "service_connection", "values_path"],
  overrides: ["DiagnosticImage", "ChartRepo"],
};

export function initConfigure() {
  state.on("config", load);
  state.on("new-config", () => { cfg = structuredClone(blank); lastPreflight = null; selectTab("configure"); render(); });
}

async function load(name) {
  blank ||= await api("/api/defaults");
  cfg = structuredClone(blank);
  lastPreflight = null;
  if (name) {
    try { cfg = (await api(`/api/configs/${encodeURIComponent(name)}`)).config; } catch { /* new */ }
    try { lastPreflight = (await api(`/api/status/${encodeURIComponent(name)}`)).status.preflight || null; } catch { /* none */ }
  }
  render();
}

const get = (path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), cfg);
function set(path, val) {
  const ks = path.split(".");
  const last = ks.pop();
  const o = ks.reduce((o, k) => (o[k] ??= {}), cfg);
  o[last] = val;
}

// Keep the values file in step with the JFrog fields (the same names the chart needs).
function derive() {
  const j = cfg.jfrog, v = cfg.values;
  const host = (j.host || "").replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  j.host = host;
  v.artifactory_url = host;
  v.provider_name = j.oidc_provider;
  if (host && j.test_repo) {
    const server = host.split(".")[0];
    j.test_repo_host = `${server}-${j.test_repo}.${host.split(".").slice(1).join(".")}`;
    v.match_images = [j.test_repo_host];
  }
  if (j.oidc_provider && !j.mapping_name) j.mapping_name = `${j.oidc_provider}-mapping`;
  cfg.template = { ...(cfg.template || {}), ClusterName: cfg.cluster.name, SubscriptionId: cfg.cluster.subscription_id,
    ArtifactoryHost: host, OidcProviderName: j.oidc_provider, MatchImages: (v.match_images || []).join(",") };
}

function field(path, label, placeholder) {
  const v = get(path);
  const id = `f-${path.replace(/\./g, "-")}`;
  const onchange = (e) => {
    const el = e.target;
    let val = el.type === "checkbox" ? el.checked : el.value.trim();
    if (Array.isArray(v)) val = val.split(",").map((x) => x.trim()).filter(Boolean);
    else if (typeof v === "number") val = Number(val);
    set(path, val);
    derive();
    validateSoon();
    renderSide();
  };
  let input;
  if (typeof v === "boolean") input = h("input", { id, type: "checkbox", checked: v, onchange });
  else if (path.endsWith(".environment") || path.endsWith("Environment")) input = h("select", { id, onchange }, ["SBX", "NPE", "PRD"].map((e) => h("option", { selected: e === v }, e)));
  else input = h("input", { id, type: "text", value: Array.isArray(v) ? v.join(", ") : v ?? "", placeholder: placeholder || "", spellcheck: "false", autocomplete: "off", onchange });
  return h("label", { class: "tfield", for: id }, h("span", {}, label || path.split(".").pop().replace(/_/g, " ")), input,
    h("span", { class: "issue small", "data-field": path }));
}

function render() {
  const k = state.knowledge;
  derive();
  const tmpl = cfg.mode === "template";
  fill(P(),
    h("h1", { class: "cfg-title" }, cfg.name && cfg.name === state.config ? cfg.name : "New cluster", null),
    h("div", { class: "cfg-layout" },
      h("div", { class: "cfg-form" },
        h("ol", { class: "rail" }, STEPS.map(([title, paths], i) => h("li", { class: `rail-step ${stepDone(paths) ? "is-done" : ""}` },
          h("h2", {}, h("span", { class: "step-n" }, String(i + 1)), title),
          i === 0 ? h("div", { class: "tgrid" }, field("name", "Configuration name", "aks-prod-eastus-01"), ...paths.map((p) => field(p, ...F[p])))
            : h("div", { class: "tgrid" }, paths.map((p) => field(p, ...F[p]))),
          i === 0 ? h("div", { class: "toolbar" }, h("button", { class: "secondary", onclick: () => discover(false) }, "Discover")) : null,
          null))),
        h("details", { class: "advanced" }, h("summary", {}, "Azure DevOps pipelines"),
          h("div", { class: "tgrid" }, PIPELINE_FIELDS.map((p) => field(p, ...F[p]))),
          h("div", { class: "mode" },
            h("label", {}, h("input", { type: "radio", name: "mode", checked: !tmpl, onchange: () => { cfg.mode = "pilot"; render(); } }), " Utility pipeline"),
            h("label", {}, h("input", { type: "radio", name: "mode", checked: tmpl, onchange: () => { cfg.mode = "template"; render(); } }), " Cluster template pipeline"))),
        h("details", { class: "advanced" }, h("summary", {}, "Advanced settings"),
          h("div", { class: "tgrid" }, Object.entries(ADVANCED).flatMap(([g, keys]) => keys.map((key) => field(`${g}.${key}`)))),
          h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!cfg.demo_mode, onchange: (e) => { cfg.demo_mode = e.target.checked; } }),
            " Demo mode"),
          tmpl ? h("div", { class: "tgrid" }, k.TEMPLATE_INPUTS.map((i) => field(`template.${i.name}`, i.name, i.default))) : null),
        h("details", { class: "advanced" }, h("summary", {}, "Generated files"),
          h("div", { id: "cfg-values" }), h("div", { id: "cfg-requests" })),
        h("div", { id: "cfg-preflight" })),
      h("aside", { class: "cfg-side", id: "cfg-side" })));
  renderSide();
  validateSoon(0);
  if (lastPreflight) showPreflight({ preflight: lastPreflight });
}

function stepDone(paths) {
  return paths.every((p) => { const v = get(p); return Array.isArray(v) ? v.length : v !== "" && v != null; });
}

// The trust chain, from the newest Preflight where there is one.
function renderSide() {
  const checks = Object.fromEntries((lastPreflight?.checks || []).map((c) => [c.id, c]));
  const st = (...ids) => {
    const cs = ids.map((i) => checks[i]).filter(Boolean);
    if (!cs.length) return "";
    return cs.every((c) => c.status === "pass") ? "is-done" : cs.some((c) => c.status === "fail") ? "is-fail" : "";
  };
  const node = (cls, title) => h("li", { class: `chain-node ${cls}` }, h("span", { class: "chain-dot" }), h("div", {}, h("b", {}, title)));
  fill($("cfg-side"),
    h("div", { class: "summary" },
      h("h2", {}, "Trust chain"),
      h("ol", { class: "chain" },
        node(st("oidc-issuer", "workload-identity", "issuer"), "AKS OIDC issuer"),
        node(st("provider"), "JFrog OIDC provider"),
        node(st("mapping"), "Identity mapping"),
        node(st("sa", "release", "writers"), "Credential provider"),
        node("is-end", "Short-lived JFrog token")),
      h("div", { class: "cfg-actions" },
        h("button", { class: "primary big", onclick: async () => { if (await save()) selectTab("install"); } }, cfg.mode === "template" ? "Run pipeline" : "Set up"),
        h("div", { class: "row-tight" },
          h("button", { class: "secondary", onclick: () => discover(true) }, "Preflight"),
          h("button", { class: "secondary", onclick: save }, "Save")),
        state.config && cfg.name === state.config ? h("button", { class: "link danger", onclick: remove }, `Delete ${state.config}`) : null,
        h("p", { class: "small muted", id: "cfg-msg" }))));
}

const filled = (path) => { const v = get(path); return Array.isArray(v) ? v.length > 0 : v !== "" && v != null; };

let timer = null;
function validateSoon(ms = 300) {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    let v;
    try { v = await api("/api/values", { method: "POST", body: { config: cfg } }); } catch { return; }
    lastValues = v;
    const issues = cfg.mode === "template" ? v.issues.concat(v.template_issues) : v.issues;
    document.querySelectorAll("#panel-configure .issue").forEach((el) => { el.textContent = ""; el.className = "issue small"; });
    for (const i of issues.filter((x) => x.level === "error" && filled(x.field))) {
      const el = document.querySelector(`#panel-configure .issue[data-field="${i.field}"]`);
      if (el && !el.textContent) { el.textContent = i.message.split(/[:(]/)[0].trim(); el.title = i.message; el.classList.add(i.level); }
    }
    fill($("cfg-values"), h("p", { class: "small muted" }, `Committed to ${v.path} · sha256 ${v.sha256.slice(0, 12)}…`),
      h("div", { class: "two-col" }, h("div", {}, h("h3", {}, "values.yaml"), h("pre", { class: "code" }, v.text)),
        h("div", {}, h("h3", {}, "Live release (helm get values -a)"), v.live ? diff(v.text, v.live) : h("p", { class: "muted small" }, "Run Discover to read the live release."))));
    fill($("cfg-requests"), h("div", { class: "two-col" },
      h("div", {}, h("h3", {}, "POST /access/api/v1/oidc"), h("pre", { class: "code" }, JSON.stringify(v.provider_request, null, 2))),
      h("div", {}, h("h3", {}, `POST …/oidc/${v.provider_name || "<provider>"}/identity_mappings`), h("pre", { class: "code" }, JSON.stringify(v.mapping_request, null, 2)))),
      h("h3", {}, "Test pod"), h("pre", { class: "code" }, v.test_pod));
  }, ms);
}

function diff(want, live) {
  const w = new Set(want.split("\n").map((l) => l.trim()));
  return h("pre", { class: "code" }, live.split("\n").map((l) => h("span", { class: l.trim() && !w.has(l.trim()) ? "diff-bad" : "" }, l + "\n")));
}

function msg(text, cls = "small") { const m = $("cfg-msg"); if (m) { m.textContent = text; m.className = cls; } }

async function save() {
  const name = (cfg.name || "").trim();
  if (!name) { msg("Give the configuration a name first.", "small error"); return false; }
  try {
    await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: cfg } });
    await refreshConfigs(name);
    state.emit("config", name);
    msg(`Saved ${name}.`, "small ok");
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
}

async function discover(pre) {
  if (!state.config || state.config !== cfg.name) { if (!(await save())) return; }
  fill($("cfg-preflight"), h("p", { class: "muted" }, pre ? "Running Preflight…" : "Discovering…"));
  try {
    const r = await api(pre ? "/api/preflight" : "/api/discover", { method: "POST", body: { config: state.config } });
    if (r.facts?.cluster?.issuer && !cfg.cluster.oidc_issuer) { cfg.cluster.oidc_issuer = r.facts.cluster.issuer; }
    if (r.facts?.nodes?.length && !cfg.cluster.nodes.length) cfg.cluster.nodes = r.facts.nodes;
    if (pre) lastPreflight = r.preflight;
    render();
    showPreflight(r);
    state.emit("status");
  } catch (e) { fill($("cfg-preflight"), h("p", { class: "error" }, e.message)); }
}

function showPreflight(r) {
  const kids = [];
  if (r.preflight) {
    kids.push(h("h2", {}, "Preflight ", pill(r.preflight.status)));
    kids.push(table(["check", "status", "detail", "fix"], r.preflight.checks.map((c) => [c.title, pill(c.status), c.detail,
      c.fallback ? h("span", {}, "Run the read-only scenario ", h("code", {}, c.fallback)) : c.fix || ""])));
  }
  if (r.facts) {
    const f = r.facts;
    kids.push(h("details", { class: "evidence" }, h("summary", {}, "Discovered"),
      table(["fact", "value"], [
        ["issuer", f.cluster.issuer || f.cluster.error || ""], ["OIDC issuer / Workload Identity", `${f.cluster.oidc_enabled} / ${f.cluster.workload_identity}`],
        ["Helm release", f.release ? `${f.release.chart} rev ${f.release.revision}` : "none"],
        ["SA annotation JFrogExchange", String(f.service_account.annotation)],
        ["JFrog DaemonSets", f.daemonsets.map((d) => `${d.namespace}/${d.name}${d.release ? "" : " (orphan)"}`).join(", ") || "none"],
        ["OIDC providers", f.providers.map((p) => p.name).join(", ") || "none read"],
        ["nodes", f.nodes.join(", ") || "not readable (Forbidden: run CHECK_KUBELET_PROVIDER)"]])));
  }
  if (r.fixture_misses) kids.push(h("p", { class: "small error" }, `Replay fixtures missing for: ${r.fixture_misses.join("; ")}`));
  fill($("cfg-preflight"), kids);
}
