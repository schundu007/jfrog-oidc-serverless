// Workload (/<cloud>/w/<name>): header with the last run's status, Run pipeline / Preflight / "⋯",
// an approval banner, and the tabs Runs · Configuration · Simulator (?tab=runs|config|sim).

import { $, api, h, fill, CLOUD, ofCloud, href, statusIcon, statusPill, statusText, template, menu, armed, ago, dur, stamp,
  runDuration, LIVE, closeFlyout, status as setStatus } from "./core.js";
import { mountConfig, validateNow, runPreflight, currentConfig, isDirty, isBusy, identityOf, kindLabel, SCOPE_FIELDS,
  status as cfgStatus } from "./configure.js";
import { mountSim, redrawSim } from "./sim.js";

const TABS = ["runs", "config", "sim"];
const ws = { name: null, schema: null, kind: null, runs: [], snaps: new Map(), poll: null, simMounted: false, tab: null };

const runsNote = (text, kind) => setStatus($("runs-status"), text, kind);

// ----------------------------------------------------------------- header

// type · scope · identity
function renderFacts(cfg) {
  const t = cfg.target?.[CLOUD] || {};
  const scope = SCOPE_FIELDS[CLOUD].filter((k) => k !== "profile" && k !== "context" && k !== "subscription").map((k) => t[k]).filter(Boolean).join(" / ");
  const idn = identityOf(cfg);
  fill($("wl-facts"),
    h("span", { text: kindLabel(ws.schema, cfg.target.kind) }),
    scope ? h("span", { class: "mono", text: scope }) : null,
    idn.text ? h("span", { class: "mono", text: idn.text }) : h("span", { text: "identity not discovered" }));
}

function renderHead() {
  const last = ws.runs[0];
  fill($("wl-pill"), last ? statusPill(last.status, `Last run ${statusText(last.status)}`) : null);
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
    cfgStatus(e.message, "error");
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
      h("a", { class: "btn secondary", href: href.list(), text: "Back to Workloads" })));
    return;
  }
  ws.kind = config.target.kind;
  // Onboarded but not saved here: the form holds the config its git workspace applied; Save keeps a copy.
  if (source === "git") {
    $("wl-menu").hidden = true;
  } else {
    fill($("wl-menu"), menu("More actions", [
      { text: "Delete workload", danger: true, confirm: `Delete ${name} and its runs?`, onSelect: removeWorkload },
    ]));
  }
  $("wl-run").addEventListener("click", runPipeline);
  $("runs-refresh").addEventListener("click", () => loadRuns().then(() => runsNote("", "")).catch((e) => runsNote(e.message, "error")));
  $("runs-clear").addEventListener("click", clearFinished);
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
