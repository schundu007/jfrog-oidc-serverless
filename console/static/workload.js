// Workload (/<cloud>/w/<name>), an Azure resource page: title, command bar (Run pipeline, Preflight,
// Refresh, "⋯"), Essentials, an approval banner, and the tabs Runs · Configuration · Simulator (?tab=runs|config|sim).

import { $, api, h, fill, nextBox, refreshing, CLOUD, ofCloud, href, statusIcon, statusText, template, menu, armed, ago, dur, stamp,
  runDuration, LIVE, closeFlyout, sectionOf, status as setStatus } from "./core.js";
import { mountConfig, validateNow, runPreflight, currentConfig, isDirty, isBusy, identityOf, kindLabel, SCOPE_FIELDS,
  status as cfgStatus } from "./configure.js";
import { mountSim, redrawSim } from "./sim.js";

const TABS = ["runs", "config", "sim"];
const ws = { cfg: null, name: null, schema: null, kind: null, runs: [], snaps: new Map(), poll: null, simMounted: false, tab: null };

const runsNote = (text, kind) => setStatus($("runs-status"), text, kind);

// ----------------------------------------------------------------- header

// Essentials, as on an Azure resource page: where it lives on the left, how it reaches JFrog on the right.
const CLOUD_NAME = { azure: "Azure", gcp: "Google Cloud" };
const pair = (label, value, extra) => [h("div", { class: "ess-pair" }, h("dt", { text: label }), h("dd", { title: typeof value === "string" ? value : null }, value || "—", extra || null))];
// Verify proves the live workload: its own identity exchanged for a JFrog token, read-only.
const verifyTone = (s) => (s === "succeeded" ? "ok" : s === "failed" || s === "cancelled" ? "fail" : "warn");
const verifyText = (s) => ({ succeeded: "Working", failed: "Failed", cancelled: "Cancelled" }[s] || "Checking");
function renderFacts(cfg) {
  if (cfg) ws.cfg = cfg;
  cfg = ws.cfg;
  if (!cfg) return;
  const t = cfg.target?.[CLOUD] || {};
  const d = cfg.target?.discovered && (!cfg.target.discovered.kind || cfg.target.discovered.kind === cfg.target.kind) ? cfg.target.discovered : {};
  const idn = identityOf(cfg);
  const last = ws.runs.find((r) => !r.verify_only && !r.prove_only) || null;
  const prv = ws.runs.find((r) => r.prove);
  const ver = ws.runs.find((r) => r.verify);
  const tone = !last ? "" : last.status === "succeeded" ? "ok" : LIVE.has(last.status) || last.status === "waiting_approval" ? "warn" : "fail";
  const where = {
    azure: [["Resource group", t.resource_group], ["Subscription", t.subscription], ["Location", d.location], ["Plan", [d.plan_tier, d.plan_sku].filter(Boolean).join(" · ")]],
    gcp: [["Project", t.project], ["Region", t.region || d.region]],
  }[CLOUD] || [];
  const url = cfg.jfrog?.url || "";
  fill($("wl-ess-left"),
    ...pair("Status", h("span", { class: `blade-state ${tone}`, text: last ? `Last run ${statusText(last.status)}` : "No pipeline runs yet" })),
    ...where.flatMap(([k, v]) => pair(k, v)));
  fill($("wl-ess-right"),
    ...pair("Verified", ver ? h("a", { class: `blade-state ${verifyTone(ver.verify.status)}`, href: href.run(ws.name, ver.id),
      text: `${verifyText(ver.verify.status)} · ${ago(ver.verify.finished || ver.created)}` }) : "Not verified yet"),
    ...pair("Proved", prv ? h("a", { class: `blade-state ${verifyTone(prv.prove.status)}`, href: href.run(ws.name, prv.id),
      text: `${{ succeeded: "Proved", failed: "Not proved", cancelled: "Cancelled" }[prv.prove.status] || "Proving"} · ${ago(prv.prove.finished || prv.created)}` }) : "Not proved yet"),
    ...pair("Identity", idn.text || "Not discovered"),
    ...(d.client_id && d.client_id !== idn.text ? pair("Client ID", d.client_id) : []),
    ...pair("JFrog platform", /^https:\/\//.test(url) ? h("a", { href: url, target: "_blank", rel: "noopener", text: url.replace(/^https:\/\//, "") }) : url),
    ...pair("Reader group", cfg.jfrog?.reader_group),
    ...(cfg.target?.kind?.startsWith("azure_") ? pair("Token audience", cfg.jfrog?.azure_audience === "token_exchange"
      ? "api://AzureADTokenExchange" : "App registration") : []),
    ...(last ? pair("Runs", h("a", { href: "?tab=runs", text: `${ws.runs.length} · last ${ago(last.created)}` })) : []));
  $("wl-sub").textContent = `${CLOUD_NAME[CLOUD] || CLOUD} · ${kindLabel(ws.schema, cfg.target.kind)}`;
  $("wl-logo").innerHTML = window.consoleNav?.icon(window.consoleNav?.section(cfg.target.kind)?.icon || CLOUD) || "";  // constant SVG only
}

function renderHead() {
  renderFacts();
  const waiting = ws.runs.filter((r) => r.status === "waiting_approval");
  const banner = $("approval-banner");
  banner.hidden = !waiting.length;
  fill(banner, waiting.length ? [statusIcon("waiting"),
    h("span", { text: `${waiting.length} approval${waiting.length === 1 ? " needs" : "s need"} review` }),
    h("a", { class: "btn secondary small-btn", href: href.run(ws.name, waiting[waiting.length - 1].id), text: "Review" })] : null);
  $("runs-count").textContent = ws.runs.length || "";
}

// ----------------------------------------------------------------- tabs

function showTab(tab, push = false) {
  if (!TABS.includes(tab)) tab = ws.runs.length ? "runs" : "config";
  ws.tab = tab;
  for (const t of TABS) {
    const a = $(`tab-${t}`);
    a.setAttribute("aria-selected", String(t === tab));
    a.tabIndex = t === tab ? 0 : -1;
    $(`panel-${t}`).hidden = t !== tab;
  }
  const url = new URL(location.href);
  url.searchParams.set("tab", tab);
  if (url.href !== location.href) history[push ? "pushState" : "replaceState"](null, "", url.pathname + url.search);
  if (tab === "sim" && !ws.simMounted) {
    ws.simMounted = true;
    mountSim($("sim-mount"), { kind: ws.kind, getConfig: currentConfig });
  } else if (tab === "sim") redrawSim();
}

function setupTabs() {
  for (const t of TABS) {
    const a = $(`tab-${t}`);
    a.setAttribute("href", `?tab=${t}`);
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      showTab(t, true);
    });
    a.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = TABS[(TABS.indexOf(t) + (e.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length];
      showTab(next, true);
      $(`tab-${next}`).focus();
    });
  }
  window.addEventListener("popstate", () => showTab(new URLSearchParams(location.search).get("tab")));
}

// ----------------------------------------------------------------- runs tab

function pips(snap) {
  if (!snap) return h("span", { class: "muted small", text: "…" });
  const steps = snap.steps.filter((s) => !s.planned);
  const done = steps.filter((s) => s.status === "succeeded").length;
  return h("span", { class: "pips", role: "img", "aria-label": `${done} of ${steps.length} steps succeeded` },
    steps.map((s) => h("i", { class: `pip pip-${s.status}`, title: `${s.title}: ${s.status.replaceAll("_", " ")}` })));
}

function runRow(r) {
  const snap = ws.snaps.get(r.id);
  const open = href.run(ws.name, r.id);
  const live = LIVE.has(r.status);
  const tr = h("tr", { class: "row-link" },
    h("td", { class: "col-icon" }, statusIcon(r.status)),
    h("th", { scope: "row" }, h("a", { class: "mono", href: open, text: r.id })),
    h("td", { title: stamp(r.created), text: ago(r.created) }),
    h("td", { class: "num", text: dur(runDuration(snap)) || "—" }),
    h("td", {}, pips(snap)),
    h("td", { class: "col-menu" }, menu(`Actions for run ${r.id}`, [
      { text: "Delete run", danger: true, disabled: live, title: live ? "Cancel the run first" : null,
        confirm: "Click again to delete", onSelect: () => deleteRun(r.id) },
    ])));
  tr.addEventListener("click", (e) => {
    if (e.target.closest("a, button, .menu-wrap")) return;
    location.assign(open);
  });
  return tr;
}

function renderRuns() {
  fill($("runs-rows"), ws.runs.map(runRow));
  $("runs-card").hidden = !ws.runs.length;
  $("runs-empty").hidden = !!ws.runs.length;
  $("runs-clear").hidden = !ws.runs.some((r) => !LIVE.has(r.status));
  renderHead();
}

// This workload's runs, newest first. Finished runs' snapshots are kept; live ones are fetched again.
async function loadRuns(onListed) {
  const { runs } = await api("/api/runs");
  ws.runs = runs.filter((r) => r.name === ws.name && ofCloud(r.kind));
  renderRuns();
  onListed?.();
  await Promise.all(ws.runs.filter((r) => LIVE.has(r.status) || !ws.snaps.has(r.id)).map(async (r) => {
    try { ws.snaps.set(r.id, await api(`/api/runs/${encodeURIComponent(r.id)}`)); } catch { /* row keeps status */ }
  }));
  renderRuns();
  clearTimeout(ws.poll);
  if (ws.runs.some((r) => LIVE.has(r.status))) ws.poll = setTimeout(() => loadRuns().catch(() => {}), 3000);
}

async function deleteRun(id) {
  try {
    await api(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" });
    ws.snaps.delete(id);
    runsNote("Run deleted", "ok");
  } catch (e) {
    runsNote(e.message, "error");
  }
  await loadRuns();
}

// Only this workload's finished runs: the API's bulk clear would take every workload of the cloud.
async function clearFinished(ev) {
  if (!armed(ev.currentTarget, "Click again to clear")) return;
  const done = ws.runs.filter((r) => !LIVE.has(r.status));
  let n = 0;
  for (const r of done) {
    try { await api(`/api/runs/${encodeURIComponent(r.id)}`, { method: "DELETE" }); n += 1; ws.snaps.delete(r.id); } catch { /* keep going */ }
  }
  runsNote(`${n} run${n === 1 ? "" : "s"} cleared`, "ok");
  await loadRuns();
}

// ----------------------------------------------------------------- actions

// Validate, Preflight (in the side panel), then start the run and open it.
async function runPipeline() {
  if (isBusy()) return;
  const v = await validateNow();
  if (v?.errors?.length) {
    showTab("config", true);
    return cfgStatus("Fix the validation errors first.", "error");
  }
  // Missing APIs, providers, roles or pipeline code stop here, not halfway through the run.
  const pre = await runPreflight();
  if (!pre || pre.status === "fail") return cfgStatus("Preflight failed: fix the checks, then Run pipeline again.", "error");
  cfgStatus("Starting the pipeline…", "busy");
  try {
    const res = await api("/api/runs", { method: "POST", body: { config: currentConfig() } });
    closeFlyout();
    location.assign(href.run(ws.name, res.run_id));
  } catch (e) {
    failed(e);
  }
}

// A refusal or failure, and what to do next (with its one-click action) beside it.
function failed(e) {
  cfgStatus(e.message, "error");
  const box = nextBox(e.next, async (steps) => {
    const res = await api("/api/runs", { method: "POST", body: { config: currentConfig(), steps } });
    location.assign(href.run(ws.name, res.run_id));
  });
  if (box) $("cfg-status").append(box);
}

// Verify: one read-only step against the live workload; the run page shows what it proved.
async function verifyNow() { return liveRun("verify", "Verifying…"); }
async function proveNow() { return liveRun("prove", "Starting the proof…"); }

async function liveRun(step, busy) {
  if (isBusy()) return;
  cfgStatus(busy, "busy");
  try {
    const res = await api("/api/runs", { method: "POST", body: { config: currentConfig(), steps: [step] } });
    location.assign(href.run(ws.name, res.run_id));
  } catch (e) {
    failed(e);
  }
}

async function removeWorkload() {
  try {
    await api(`/api/configs/${encodeURIComponent(ws.name)}`, { method: "DELETE" });
    location.assign(href.list());
  } catch (e) {
    cfgStatus(e.message, "error");
  }
}

// ----------------------------------------------------------------- mount

// Refresh: the saved config (discovered facts) and every run read again; unsaved edits stay.
async function refreshAll() {
  cfgStatus("Refreshing…", "busy");
  try {
    const [{ config }] = await Promise.all([
      isDirty() ? Promise.resolve({ config: null }) : api(`/api/configs/${encodeURIComponent(ws.name)}`),
      (ws.snaps.clear(), loadRuns())]);
    if (config) renderFacts(config);
    cfgStatus(`Refreshed ${stamp(new Date().toISOString())}`, "ok");
  } catch (e) {
    cfgStatus(`Refresh failed: ${e.message}`, "error");
    throw e;
  }
}

export async function mountWorkload(page, { name, schema }) {
  Object.assign(ws, { name, schema });
  page.replaceChildren(template("tpl-workload"));
  $("wl-title").textContent = name;
  document.title = `${name} · Workloads`;
  let config, source;
  try {
    ({ config, source } = await api(`/api/configs/${encodeURIComponent(name)}`));
  } catch (e) {
    config = null;
    if (e.status !== 404) throw e;
  }
  if (!config || !ofCloud(config.target?.kind)) {
    fill(page, h("div", { class: "empty" }, h("h2", { text: `No workload named ${name}` }),
      h("a", { class: "btn secondary", href: href.list(), text: "Back to workloads" })));
    return;
  }
  ws.kind = config.target.kind;
  ws.cfg = config;
  document.dispatchEvent(new CustomEvent("console:section", { detail: sectionOf(ws.kind) }));
  // Onboarded but not saved here: the form holds the config its git workspace applied; Save keeps a copy.
  if (source === "git") {
    $("wl-menu").hidden = true;
  } else {
    fill($("wl-menu"), menu("More actions", [
      { text: "Delete workload", danger: true, confirm: `Delete ${name} and its runs?`, onSelect: removeWorkload },
    ]));
  }
  $("wl-run").addEventListener("click", runPipeline);
  $("wl-verify").addEventListener("click", verifyNow);
  $("wl-prove").addEventListener("click", proveNow);
  refreshing($("runs-refresh"), () => loadRuns().then(() => runsNote("", "")).catch((e) => { runsNote(e.message, "error"); throw e; }));
  $("runs-clear").addEventListener("click", clearFinished);
  refreshing($("wl-refresh"), refreshAll);
  $("wl-ess-h").addEventListener("click", () => {
    const open = $("wl-ess-h").getAttribute("aria-expanded") !== "true";
    $("wl-ess-h").setAttribute("aria-expanded", String(open));
    $("wl-ess-body").hidden = !open;
  });
  await mountConfig($("cfg-mount"), { schema, mode: "edit", name, config, onChange: renderFacts });
  if (source === "git") cfgStatus("Read from git (onboarded). Save keeps a copy here.", "");
  setupTabs();
  window.addEventListener("beforeunload", (e) => { if (isDirty()) e.preventDefault(); });
  const first = () => showTab(new URLSearchParams(location.search).get("tab"));
  try {
    await loadRuns(first);
  } catch (e) {
    first();
    runsNote(`Could not list runs: ${e.message}`, "error");
  }
}
