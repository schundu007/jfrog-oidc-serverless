// Clusters: pick or add a cluster, see its live state, and run Set up / Verify / Tear down.
// Each action runs itself: dry run, then the plan for one approval, then the real run, then a fresh
// status read and, on a failure, the diagnosis. The approval is the only click inside a run.

import { api, h, fill, state, selectTab, refreshConfigs } from "./app.js";
import { $, pill, table, stream, workflow, brief } from "./ui.js";

let aks = null;             // GET /api/aks
const live = {};            // config name -> {loading, facts, preflight, error}
let flow = null;            // the running action: {which, el, running}

const TITLES = { setup: "Set up", verify: "Verify", teardown: "Tear down" };

export function initOverview() {
  state.on("config", (name) => { if (!flow || !flow.running) flow = null; render(); if (name && !live[name]) readStatus(name); });
  render();
  if (state.config) readStatus(state.config);
}

const muted = (t) => h("span", { class: "muted" }, t);
const when = (iso) => (iso || "").slice(0, 16).replace("T", " ");

function render() {
  const P = $("panel-overview");
  const configs = state.configs || [];
  if (!aks) loadAks();
  const cur = configs.find((c) => c.name === state.config);
  fill(P,
    h("div", { class: "cfg-head" }, h("div", {}, h("h1", {}, "Clusters"))),
    configs.length ? savedTable(configs) : null,
    cur ? clusterCard(cur) : null,
    aksTable(configs));
}

function savedTable(configs) {
  return h("table", { class: "cfg-table clickable" },
    h("thead", {}, h("tr", {}, ["Name", "AKS cluster", "State", "Updated"].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, configs.map((c) => h("tr", { class: c.name === state.config ? "is-open" : "", onclick: () => select(c.name) },
      h("td", {}, h("b", {}, c.name)), h("td", {}, c.cluster || muted("—")),
      h("td", {}, stateText(live[c.name])), h("td", { class: "muted small" }, when(c.updated))))));
}

// Installed, not installed, or the first problem Preflight found.
function stateText(s) {
  if (!s || s.loading) return muted(s ? "reading…" : "—");
  if (s.error) return h("span", { class: "error small" }, brief(s.error));
  const bad = (s.preflight?.checks || []).find((c) => c.status === "fail");
  if (bad) return h("span", { class: "pill fail" }, bad.title);
  return s.facts?.release ? h("span", { class: "pill pass" }, "installed") : muted("not installed");
}

function clusterCard(c) {
  const s = live[c.name] || {};
  const f = s.facts || {};
  const rel = f.release;
  const ds = (f.daemonsets || []).find((d) => /jfrog/i.test(d.name));
  const facts = [
    ["Kubernetes", f.cluster?.kubernetes_version || "—"],
    ["OIDC issuer · Workload Identity", f.cluster ? `${f.cluster.oidc_enabled ? "on" : "off"} · ${f.cluster.workload_identity ? "on" : "off"}` : "—"],
    ["Helm release", rel ? `${rel.chart} · rev ${rel.revision} · ${rel.status}` : "not installed"],
    ["DaemonSet", ds ? "installed" : "—"],
    ["Service account", f.service_account ? (f.service_account.exists ? `JFrogExchange: ${f.service_account.annotation ?? "missing"}` : "not created") : "—"],
    ["Nodes", f.nodes ? String(f.nodes.length) : "—"],
    ["JFrog OIDC provider", f.providers ? ((f.providers || []).some((p) => p.name === c.provider) ? "present" : "missing") : "—"],
  ];
  const problems = (s.preflight?.checks || []).filter((x) => x.status === "fail" || x.status === "warn");
  const busy = !!(flow && flow.running);
  return h("section", { class: "summary cluster-card" },
    h("div", { class: "toolbar" },
      h("h2", {}, c.name, muted(` · ${c.cluster || "—"}`)),
      h("div", { class: "tools" },
        h("button", { class: "secondary", disabled: s.loading || busy, onclick: () => readStatus(c.name) }, s.loading ? "Reading…" : "Refresh"),
        h("button", { class: "secondary", disabled: busy, onclick: () => selectTab("configure") }, "Edit"),
        h("button", { class: "secondary", disabled: busy || !rel, onclick: () => start("teardown") }, "Tear down"),
        h("button", { class: "secondary", disabled: busy || !rel, onclick: () => start("verify") }, "Verify"),
        h("button", { class: "primary", disabled: busy, onclick: () => start("setup") }, rel ? "Set up again" : "Set up"))),
    s.error ? h("p", { class: "error small" }, brief(s.error)) : null,
    problems.length ? table(["", "check", "detail"], problems.map((x) => [pill(x.status), x.title, brief(x.detail)])) : null,
    proofBlock(s.proof),
    table(["", ""], facts, "grid small facts"),
    flow ? flow.el : null);
}

// The OIDC proof: each link of the newest real Set up or Verify that checked it, cluster to JFrog.
const PROOF = ["cluster", "sa-check", "trust", "map", "mapping", "provider", "anon", "pull", "who"];
function latestProof(runs, name) {
  const run = runs.find((x) => x.config === name && x.kind === "direct" && x.name !== "teardown"
    && x.request.parameters.DryRun === false && ((x.result || {}).steps || []).some((st) => st.id === "pull"));
  if (!run) return null;
  const steps = run.result.steps.filter((st) => PROOF.includes(st.id)).sort((a, b) => PROOF.indexOf(a.id) - PROOF.indexOf(b.id));
  const proven = run.status === "succeeded" && steps.some((st) => st.id === "who" && st.status === "ok")
    && !steps.some((st) => st.status === "failed" || st.status === "warn");
  return { at: run.created, steps, proven, complete: steps.some((st) => st.id === "who") };
}

function proofBlock(p) {
  const head = (cls, text) => h("div", { class: "toolbar" }, h("h3", {}, "OIDC"), h("span", { class: `pill ${cls}` }, text));
  if (!p) return h("div", { class: "proof" }, head("notrun", "not checked"));
  const verdict = p.proven ? ["pass", `working · ${when(p.at)}`] : !p.complete ? ["notrun", "run Verify"] : ["fail", `not proven · ${when(p.at)}`];
  return h("div", { class: "proof" }, head(...verdict),
    table(["", "check", "result"], p.steps.map((st) => [pill(st.status === "done" ? "ok" : st.status), st.title || st.id, brief(st.detail)])));
}

// One read per cluster at a time: a second call while one is in flight joins it.
const reading = {};
function readStatus(name) {
  return (reading[name] ||= doRead(name).finally(() => { delete reading[name]; }));
}

async function doRead(name) {
  live[name] = { ...(live[name] || {}), loading: true, error: "" };
  render();
  try {
    const [r, { runs }] = await Promise.all([api("/api/preflight", { method: "POST", body: { config: name } }), api("/api/runs")]);
    live[name] = { facts: r.facts, preflight: r.preflight, proof: latestProof(runs, name) };
  } catch (err) {
    live[name] = { error: err.message };
  }
  render();
}

// ----------------------------------------------------------------------- the automated action

function start(which) {
  const el = h("div", { class: "flow" });
  flow = { which, el, running: true };
  render();
  runFlow(which, el).finally(() => { flow.running = false; render(); });
}

function stage(el, text, cls = "") {
  const li = h("li", { class: cls }, text);
  el.querySelector("ol.stages").append(li);
  return li;
}

const set = (li, text, cls) => { li.textContent = text; li.className = cls; };

async function runFlow(which, el) {
  const name = state.config;
  fill(el, h("h3", {}, TITLES[which]), h("ol", { class: "stages" }));
  // 1. Dry run: every step checked against the cluster and JFrog, nothing changed.
  const s1 = stage(el, "Dry run…");
  const dry = await execute(name, which, true);
  if (!dry.ok) return failed(el, s1, "Dry run failed", dry, name);
  set(s1, "Dry run passed", "ok");
  // 2. One approval for the exact request (its sha256 binds the plan).
  const s2 = stage(el, "Waiting for approval");
  let real;
  try {
    real = await api("/api/runs", { method: "POST", body: { config: name, kind: "direct", name: which, params: { DryRun: false } } });
  } catch (err) { return failed(el, s2, err.message, null, name); }
  if (real.guards.length) return failed(el, s2, real.guards.join(" "), null, name);
  if (!(await approval(el, real, dry.result))) { set(s2, "Cancelled", "muted"); return; }
  set(s2, "Approved", "ok");
  // 3. Apply, with the live log.
  const s3 = stage(el, "Applying…");
  const log = h("ol", { class: "events small" });
  el.append(log);
  const done = await follow(real.id, log);
  log.remove();
  if (done.status !== "succeeded") return failed(el, s3, "Failed", done, name);
  set(s3, which === "teardown" ? "Removed" : "Applied", "ok");
  el.append(stepTable(done.result));
  // 4. Fresh state.
  await readStatus(name);
}

async function execute(name, which, dryRun) {
  try {
    const run = await api("/api/runs", { method: "POST", body: { config: name, kind: "direct", name: which, params: { DryRun: dryRun } } });
    if (run.guards.length) return { ok: false, error: run.guards.join(" ") };
    await api(`/api/runs/${run.id}/approve`, { method: "POST", body: { request_sha256: run.request_sha256, approved: true } });
    const done = await follow(run.id, null);
    return { ok: done.status === "succeeded", ...done };
  } catch (err) { return { ok: false, error: err.message }; }
}

async function follow(id, log) {
  let out = { status: "unknown" };
  await stream(`/api/runs/${id}/events`, (kind, data) => {
    if (kind === "event" && log && workflow(data.text)) log.append(h("li", {}, h("span", { class: "mono muted" }, data.t.slice(11, 19), " "), brief(data.text)));
    if (kind === "done") out = data;
  });
  return out;
}

// The plan: what changes, with what the dry run saw, and one approve button
// (plus the typed cluster name for a tear down).
function approval(el, run, dryResult) {
  return new Promise((resolve) => {
    const dryById = Object.fromEntries(((dryResult || {}).steps || []).map((s) => [s.id, s]));
    const typed = h("input", { type: "text", class: "typed", placeholder: run.cluster, autocomplete: "off", "aria-label": "Type the cluster name" });
    const go = h("button", { class: "primary" }, run.needs_typed_cluster ? "Approve and tear down" : "Approve and apply");
    const no = h("button", { class: "secondary" }, "Cancel");
    const box = h("div", { class: "approve-box" },
      table(["", "step", "dry run"], run.request.parameters.steps.map((s, i) => [String(i + 1), s.title,
        dryById[s.id] ? h("span", {}, pill(dryById[s.id].status), " ", brief(dryById[s.id].detail)) : ""])),
      h("div", { class: "approve" }, run.needs_typed_cluster ? typed : null, go, no));
    el.append(box);
    no.onclick = () => { box.remove(); resolve(false); };
    go.onclick = async () => {
      go.disabled = true;
      try {
        await api(`/api/runs/${run.id}/approve`, { method: "POST",
          body: { request_sha256: run.request_sha256, approved: true, typed_cluster: typed.value } });
        box.remove();
        resolve(true);
      } catch (err) {
        go.disabled = false;
        box.append(h("p", { class: "error small" }, err.message));
      }
    };
  });
}

function stepTable(result) {
  return table(["", "step", "detail"], ((result || {}).steps || []).map((s) => [pill(s.status === "done" ? "ok" : s.status), s.title || s.id, brief(s.detail)]));
}

// A failed stage: the step results, then the cause from the run evidence, then a fresh status.
async function failed(el, li, text, run, name) {
  set(li, text, "error");
  if (run && run.error) el.append(h("p", { class: "error small" }, brief(run.error)));
  if (run && run.result) el.append(stepTable(run.result));
  try {
    const d = await api("/api/diagnose", { method: "POST", body: { config: name, text: "" } });
    const top = (d.findings || []).find((f) => f.level === "cause");
    if (top) el.append(h("div", { class: "callout fail" }, h("strong", {}, "Cause: "), top.title));
  } catch { /* the step detail above stands */ }
  await readStatus(name);
}

// ----------------------------------------------------------------------- add a cluster

function aksTable(configs) {
  const added = new Set(configs.map((c) => (c.cluster || "").toLowerCase()));
  const head = h("div", { class: "toolbar" }, h("h2", {}, "Add an AKS cluster"),
    h("div", { class: "tools" }, h("button", { class: "secondary", onclick: () => { aks = null; render(); } }, "Refresh")));
  if (!aks || aks.loading) return h("div", {}, head, muted("Reading az aks list…"));
  if (aks.error) return h("div", {}, head, h("p", { class: "error small" }, brief(aks.error)));
  const rows = aks.clusters.filter((k) => !added.has(k.name.toLowerCase()));
  if (!rows.length) return null;
  return h("div", {}, head, h("table", { class: "cfg-table" },
    h("thead", {}, h("tr", {}, ["Cluster", "Resource group", "Location", "Kubernetes", "OIDC · WI", ""].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, rows.map((k) => h("tr", {},
      h("td", {}, h("b", {}, k.name), k.power && k.power !== "Running" ? muted(` ${k.power}`) : null),
      h("td", {}, k.resource_group), h("td", {}, k.location), h("td", {}, k.version),
      h("td", {}, `${k.oidc_issuer ? "on" : "off"} · ${k.workload_identity ? "on" : "off"}`),
      h("td", { class: "row-actions" }, h("button", { class: "primary small-btn", onclick: () => add(k) }, "Add")))))));
}

async function loadAks() {
  aks = { clusters: [], loading: true };
  try { aks = await api("/api/aks"); } catch (err) { aks = { clusters: [], error: err.message }; }
  render();
}

// A new configuration: the cluster from az, the JFrog side copied from the newest saved cluster
// (same JFrog instance, user and test image), the provider and mapping named after the cluster.
async function add(k) {
  const cfg = await api("/api/defaults");
  const name = k.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 63);
  cfg.name = name;
  Object.assign(cfg.cluster, { name: k.name, resource_group: k.resource_group, subscription_id: k.subscription_id, oidc_issuer: k.oidc_issuer });
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
  aks = null;
  await select(name);
  if (!cfg.jfrog.host) selectTab("configure");
}

async function select(name) {
  await refreshConfigs(name);
  state.emit("config", name);
}
