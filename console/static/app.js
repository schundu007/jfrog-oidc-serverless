// JFrog OIDC console UI. Vanilla ES module, no build step, no CDN.
// The session token comes from the URL fragment and is sent only as the X-Console-Token header.
// It is kept in localStorage for this origin (127.0.0.1:<port>) so reloads and new tabs work:
// never in a cookie or the URL. The console keeps the same token across restarts (--new-token replaces it).

import { renderTimeline, hhmm, LEGEND } from "./timeline.js";
import { initConfigure } from "./configure.js";
import { initRuns } from "./runs.js";

const $ = (id) => document.getElementById(id);

// One origin only: the saved sign-in lives in 127.0.0.1's storage, so send localhost there.
// Nothing else runs on localhost, so the token in the fragment travels with the redirect.
const REDIRECTING = location.hostname === "localhost";
if (REDIRECTING) location.replace(location.href.replace("//localhost", "//127.0.0.1"));

// Inside the portal this console lives under a prefix (/serverless): the server says so in a meta tag.
// Every API call, static file and page link carries it; localPath() is the path without it.
const BASE = document.querySelector('meta[name="console-base"]')?.content || "";
const localPath = () => (BASE && location.pathname.startsWith(BASE) ? location.pathname.slice(BASE.length) || "/" : location.pathname);

// Google Cloud and Azure are separate pages (/gcp, /azure; ?cloud= still works); nothing on one
// shows the other. Each tab has its own path: /<cloud>/configs|configure|runs|simulate.
const CLOUDS = { gcp: "Google Cloud", azure: "Azure" };
const CLOUD = [localPath().split("/")[1], new URLSearchParams(location.search).get("cloud")]
  .find((c) => Object.hasOwn(CLOUDS, c)) || null;
const ofCloud = (kind) => !!kind && String(kind).startsWith(`${CLOUD}_`);

// replaceChildren() would turn null children into the text "null"; drop them.
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

// ----------------------------------------------------------------- session

// Read the token from the fragment and drop the fragment from the address bar
// and history straight away. Also runs on hashchange: pasting the URL into an
// open tab (e.g. after a console restart) is a same-document navigation.
function takeTokenFromHash() {
  const m = location.hash.match(/(?:^#|&)t=([A-Za-z0-9_-]+)/);
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  return m ? m[1] : null;
}

// Keep it so a reload or a new tab does not lock the operator out. It is only valid while
// this console process runs. A token from the URL always wins (console restart).
const TOKEN_KEY = "console-session-token";
function rememberToken(t) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); } catch { /* storage blocked: memory only */ }
  return t;
}
function storedToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

// The server puts the token in the page when the operator opened it directly (no #t= needed).
function pageToken() {
  const m = document.querySelector('meta[name="console-token"]');
  const t = m ? m.content : null;
  if (m) m.remove();
  return t;
}

let TOKEN = REDIRECTING ? null : rememberToken(takeTokenFromHash() || pageToken()) || storedToken();

class ApiError extends Error {
  constructor(status, message, code) { super(code ? `${message} (${code})` : message); this.status = status; this.code = code; }
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Console-Token": TOKEN };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit", cache: "no-store" });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || `HTTP ${resp.status}`, data && data.code);
  return data;
}

// Server-sent events over fetch, so the token stays in a header (EventSource can't set headers).
async function streamEvents(path, onFrame, signal) {
  const resp = await fetch(BASE + path, { headers: { "X-Console-Token": TOKEN }, credentials: "omit", cache: "no-store", signal });
  if (!resp.ok || !resp.body) throw new ApiError(resp.status, `event stream failed: HTTP ${resp.status}`);
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let kind = "message";
      const data = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) kind = line.slice(7);
        else if (line.startsWith("data: ")) data.push(line.slice(6));
      }
      if (data.length) onFrame(kind, JSON.parse(data.join("\n")));
    }
  }
}

// ----------------------------------------------------------------- dom helpers

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function showError(msg) {
  const e = $("form-error");
  e.textContent = msg || "";
  e.hidden = !msg;
}

// ----------------------------------------------------------------- tabs

function setupTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab, focus = true, how = "push") => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
    const path = BASE + (CLOUD ? `/${CLOUD}/${tab.id.slice("tab-".length)}` : "/");
    if (how !== "none" && location.pathname !== path) history[how === "replace" ? "replaceState" : "pushState"](null, "", path);
  };
  state.selectTab = (id, how = "push") => select($(id), false, how);
  window.addEventListener("popstate", () => {
    const t = $(`tab-${localPath().split("/")[2] || ""}`);
    if (t && tabs.includes(t)) select(t, false, "none");
  });
  tabs.forEach((t) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      // Hidden tabs (e.g. Configurations with nothing saved) are skipped.
      const shown = tabs.filter((x) => !x.hidden);
      const j = shown.indexOf(t);
      const d = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (d) { e.preventDefault(); select(shown[(j + d + shown.length) % shown.length]); }
      if (e.key === "Home") { e.preventDefault(); select(shown[0]); }
      if (e.key === "End") { e.preventDefault(); select(shown[shown.length - 1]); }
    });
  });
}

// ----------------------------------------------------------------- simulate form

const LOAD_SPEC = {
  cold_starts_per_hour: { label: "Cold starts / hour", min: 0, max: 120, step: 1 },
  requests_per_minute: { label: "Requests / minute", min: 0, max: 120, step: 1 },
  uncached_pull_ratio: { label: "Uncached pull ratio (GCP AR)", min: 0, max: 1, step: 0.05 },
  instance_lifetime_minutes: { label: "Instance lifetime (min)", min: 5, max: 240, step: 5 },
};

const SETTING_LABELS = {
  "pull_path.ttl_seconds": "Pull TTL (s)",
  "pull_path.rotation_cron": "Rotation cron",
  "pull_path.alert_after_seconds": "Alert after (s)",
  "pull_path.min_ttl_seconds": "MIN_TTL_SECONDS",
  "runtime.ttl_seconds": "Runtime TTL (s)",
};

// Plain names for the simulator's fault types; each page lists only its cloud's faults.
const FAULT_LABELS = {
  jfrog_outage: "JFrog outage", rotator_down: "Rotator can't get its identity",
  identity_recreated: "App identity recreated", sa_recreated: "Service account recreated",
  arm_refresh_forbidden: "Key Vault refresh forbidden", mapping_revoked: "JFrog mapping removed",
  integration_misconfigured: "JFrog trust has the wrong issuer",
};
const CLOUD_ONLY_FAULTS = { arm_refresh_forbidden: "azure", sa_recreated: "gcp" };
const faultLabel = (t) => FAULT_LABELS[t] || t.replaceAll("_", " ");
const KIND_LABELS = {
  gcp_run_service: "Cloud Run service", gcp_run_job: "Cloud Run job", gcp_run_function: "Cloud Run function",
  azure_container_app: "Container App", azure_functions_aca: "Functions on Container Apps",
  azure_functions_premium: "Functions Premium", azure_functions_flex: "Functions Flex Consumption",
};
const scenarioLabel = (s) => {
  const words = s.name.replaceAll("-", " ");
  return `${words[0].toUpperCase()}${words.slice(1)}`;
};

const state = { catalogue: null, scenario: null, running: false, events: [], result: null };

function scenarioByName(name) {
  return state.catalogue.scenarios.find((s) => s.name === name);
}

function renderOverrides(s) {
  const box = $("overrides");
  box.replaceChildren();
  for (const [key, value] of Object.entries(s.settings)) {
    const numeric = typeof value === "number";
    const input = h("input", {
      id: `ov-${key}`, "data-key": key, "data-default": String(value), value: String(value),
      type: numeric ? "number" : "text", spellcheck: "false", ...(numeric ? { min: 0, step: 60 } : {}),
    });
    box.append(h("label", { class: "field" }, h("span", { text: SETTING_LABELS[key] || key }), input));
  }
}

function faultRow(f = {}) {
  const types = Object.keys(state.catalogue.fault_types);
  const type = h("select", { "aria-label": "Fault type" },
    types.map((t) => h("option", { value: t, text: faultLabel(t), selected: t === f.type, title: state.catalogue.fault_types[t] })));
  const start = h("input", { type: "text", value: f.start ?? "03:00", placeholder: "HH:MM", "aria-label": "Fault start (HH:MM)", size: 6 });
  const end = h("input", { type: "text", value: f.end ?? "", placeholder: "end", "aria-label": "Fault end (HH:MM, empty = until the end)", size: 6 });
  const params = h("input", { type: "text", value: f.params && Object.keys(f.params).length ? JSON.stringify(f.params) : "",
    placeholder: "params JSON", "aria-label": "Fault params as JSON", spellcheck: "false", class: "params" });
  const row = h("div", { class: "fault-row" }, type, start, h("span", { class: "muted", text: "→" }), end, params,
    h("button", { type: "button", class: "icon", "aria-label": "Remove fault", text: "✕", onclick: () => row.remove() }));
  row.collect = () => {
    const out = { type: type.value, start: start.value.trim() || "00:00" };
    if (end.value.trim()) out.end = end.value.trim();
    if (params.value.trim()) {
      try { out.params = JSON.parse(params.value); } catch { throw new Error(`fault ${type.value}: params is not valid JSON`); }
    }
    return out;
  };
  return row;
}

function renderFaults(s) {
  $("faults").replaceChildren(...(s.faults || []).map(faultRow));
}

function renderLoad(s) {
  const box = $("load");
  box.replaceChildren();
  const load = { ...state.catalogue.default_load, ...(s.load || {}) };
  for (const [key, spec] of Object.entries(LOAD_SPEC)) {
    const out = h("output", { text: String(load[key]) });
    const input = h("input", { type: "range", id: `ld-${key}`, "data-key": key, min: spec.min, max: spec.max, step: spec.step, value: load[key],
      oninput: () => { out.textContent = input.value; } });
    out.setAttribute("for", input.id);
    box.append(h("label", { class: "slider" }, h("span", { text: spec.label }), input, out));
  }
}

function selectScenario(name) {
  const s = scenarioByName(name);
  state.scenario = s;
  const exp = (s.expect && s.expect.verdict) || "—";
  $("scenario").title = s.description || "";
  $("scenario-meta").replaceChildren(h("span", { class: "field-label", text: "Expected" }),
    h("span", { class: `pill v-${exp}`, text: exp }));
  renderOverrides(s);
  renderFaults(s);
  renderLoad(s);
  const dur = $("duration");
  const want = s.duration || "24h";
  if (![...dur.options].some((o) => o.value === want)) dur.append(h("option", { value: want, text: want }));
  dur.value = want;
  $("seed").value = "1";
  showError("");
}

function collectRequest() {
  const body = { scenario: state.scenario.name, overrides: {}, load: {}, faults: [] };
  for (const input of $("overrides").querySelectorAll("input")) {
    const raw = input.value.trim();
    if (raw === input.dataset.default) continue;
    const numeric = input.type === "number";
    if (numeric && (raw === "" || !Number.isFinite(Number(raw)))) throw new Error(`${input.dataset.key} must be a number`);
    body.overrides[input.dataset.key] = numeric ? Number(raw) : raw;
  }
  for (const input of $("load").querySelectorAll("input")) body.load[input.dataset.key] = Number(input.value);
  for (const row of $("faults").children) body.faults.push(row.collect());
  body.duration = $("duration").value;
  const seed = $("seed").value.trim();
  if (seed !== "") body.seed = Number.parseInt(seed, 10);
  return body;
}

// ----------------------------------------------------------------- run + results

function setRunning(on) {
  state.running = on;
  $("run").disabled = on;
  $("run").textContent = on ? "Running…" : "Run simulation";
  $("progress").hidden = !on;
}

function progress(frac, text) {
  // CSSOM writes are allowed under the CSP; inline style attributes are not.
  $("progress-bar").style.width = `${Math.round(Math.min(1, Math.max(0, frac)) * 100)}%`;
  $("progress-text").textContent = text;
}

async function runSimulation(ev) {
  ev.preventDefault();
  if (state.running) return;
  showError("");
  let body;
  try { body = collectRequest(); } catch (e) { showError(e.message); return; }
  await startSim(body);
}

async function startSim(body) {
  setRunning(true);
  state.events = [];
  progress(0, "queued");
  let duration = 1;
  try {
    const { sim_id } = await api("/api/sim", { method: "POST", body });
    let final = null;
    let failure = null;
    await streamEvents(`/api/sim/${encodeURIComponent(sim_id)}/events`, (kind, data) => {
      if (kind === "status") { duration = data.duration_s || 1; progress(0, data.status); }
      else if (kind === "event") {
        state.events.push(data);
        if (state.events.length % 25 === 0) progress(data.t / duration, `${hhmm(Math.max(0, data.t))} simulated · ${state.events.length} events`);
      } else if (kind === "done") final = data;
      else if (kind === "failed") failure = data.error;
    });
    if (failure) throw new Error(`simulation failed: ${failure}`);
    if (!final) throw new Error("event stream ended before the simulation finished");
    const { result } = await api(`/api/sim/${encodeURIComponent(sim_id)}`);
    state.result = result;
    renderResult(result);
  } catch (e) {
    showError(e.status === 401 ? "Session token rejected. Open the URL the console printed." : e.message);
  } finally {
    setRunning(false);
  }
}

function stat(label, value, cls = "") {
  return h("div", { class: `stat ${cls}` }, h("span", { class: "stat-v", text: value }), h("span", { class: "stat-l", text: label }));
}

function renderVerdict(r) {
  const s = r.summary;
  const m = r.ttl_math || {};
  const expected = state.scenario && state.scenario.name === r.scenario ? state.scenario.expect?.verdict : null;
  const box = $("verdict");
  box.className = `card verdict v-${r.verdict}`;
  const failCls = (n) => (n > 0 ? "bad" : "");
  fill(box,
    h("div", { class: "verdict-head" },
      h("span", { class: `badge big ${r.verdict}`, text: r.verdict.toUpperCase() }),
      h("div", {},
        h("h2", { text: r.scenario }),
        h("p", { class: "muted small" }, `${Math.round(r.duration_s / 3600)} h · seed ${r.seed}`,
          expected ? h("span", { class: expected === r.verdict ? "match" : "mismatch",
            text: expected === r.verdict ? " · as expected" : ` · expected ${expected}` }) : null))),
    h("div", { class: "stats" },
      stat("Rotations ok / failed", `${s.rotations_ok} / ${s.rotations_failed}`, failCls(s.rotations_failed)),
      stat("Pulls ok / cached / failed", `${s.pulls_ok} / ${s.pulls_cached} / ${s.pulls_failed}`, failCls(s.pulls_failed)),
      stat("Requests ok / failed", `${s.requests_ok + s.requests_ok_cached} / ${s.requests_failed + s.requests_unavailable}`,
        failCls(s.requests_failed + s.requests_unavailable)),
      stat("Exposure", `${Math.round(s.exposure_s / 60)} min`, failCls(s.exposure_s)),
      stat("Alert", s.alert_first_fired_s == null ? "—" : hhmm(s.alert_first_fired_s)),
      m.pull_ttl_seconds ? stat("Pull TTL / min safe", `${m.pull_ttl_seconds} / ${m.min_safe_ttl_seconds} s`,
        m.pull_ttl_seconds < m.min_safe_ttl_seconds ? "bad" : "") : null,
      m.pull_ttl_seconds ? stat("Missed rotations survived", String(m.survives_missed_rotations)) : null),
    r.root_cause ? h("div", { class: "root-cause" }, h("h3", { text: "Root cause" }), h("p", { text: r.root_cause })) : null,
    r.chain && r.chain.length ? h("details", {}, h("summary", { text: "Causal chain" }),
      h("ol", { class: "chain" }, r.chain.map((c) => h("li", { text: c })))) : null);
}

function renderEvents() {
  const type = $("ev-type").value;
  const q = $("ev-search").value.trim().toLowerCase();
  const rows = state.events.filter((e) => (!type || e.type === type) && (!q || e.message.toLowerCase().includes(q)));
  const shown = rows.slice(0, 2000);
  $("events").tBodies[0].replaceChildren(...shown.map((e) =>
    h("tr", { class: `ev-${e.type}${/fail|error|401|403|refus|FIRING|expired/i.test(e.message) ? " ev-bad" : ""}` },
      h("td", { class: "mono", text: hhmm(e.t) }), h("td", { text: e.type }), h("td", { class: "msg", text: e.message }))));
  $("ev-title").textContent = `Events (${state.events.length})`;
  $("ev-count").textContent = `${rows.length} of ${state.events.length} events` + (rows.length > shown.length ? ` (first ${shown.length} shown)` : "");
}

function renderResult(r) {
  $("sim-results").hidden = false;
  renderVerdict(r);
  renderTimeline($("timeline"), r);
  state.events = r.events || state.events;
  const types = [...new Set(state.events.map((e) => e.type))].sort();
  const sel = $("ev-type");
  const keep = sel.value;
  sel.replaceChildren(h("option", { value: "", text: "All kinds" }), ...types.map((t) => h("option", { value: t, text: t })));
  sel.value = types.includes(keep) ? keep : "";
  renderEvents();
}

// ----------------------------------------------------------------- boot

// Shown only when something is wrong.
function sessionState(text, ok) {
  const pill = $("session-state");
  pill.textContent = text;
  pill.hidden = ok;
}

// Load the catalogue with the current token. Returns false (and says why) on failure.
async function connect() {
  if (!TOKEN) {
    $("auth-banner").hidden = false;
    sessionState("no token", false);
    $("run").disabled = true;
    return false;
  }
  try {
    state.catalogue = await api("/api/sim/scenarios");
  } catch (e) {
    sessionState(e.status === 401 ? "token rejected" : "offline", false);
    $("auth-banner").hidden = e.status !== 401;
    if (e.status === 401) { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } }
    showError(e.message);
    $("run").disabled = true;
    return false;
  }
  $("auth-banner").hidden = true;
  showError("");
  $("run").disabled = false;
  sessionState("local session", true);
  return true;
}

// Keep only this cloud's fields and links, then show the picker or the tabs.
function applyCloud() {
  if (BASE) {
    // Portal: this console is the Serverless section; links to its own pages carry the prefix,
    // and the switch to the Kubernetes section (a different console) is a full page load.
    for (const a of document.querySelectorAll('a[href^="/"]:not([data-section])')) a.setAttribute("href", BASE + a.getAttribute("href"));
    for (const el of document.querySelectorAll("[data-portal]")) el.hidden = false;
  }
  document.title = CLOUD ? `JFrog OIDC Console · ${CLOUDS[CLOUD]}` : "JFrog OIDC Console";
  if (!CLOUD) {
    $("panel-pick").hidden = false;
    return;
  }
  for (const el of document.querySelectorAll("[data-cloud]")) if (el.dataset.cloud !== CLOUD) el.remove();
  for (const el of document.querySelectorAll(".crumb-cloud")) el.textContent = CLOUDS[CLOUD];
  $("ctx-cloud").textContent = CLOUDS[CLOUD];
  $("ctx-cloud").hidden = false;
  for (const a of document.querySelectorAll("[data-cloud-link]")) {
    if (a.dataset.cloudLink === CLOUD) a.setAttribute("aria-current", "page");
  }
  $("cloud-switch").hidden = false;
  $("tabs").hidden = false;
  const want = $(`tab-${localPath().split("/")[2] || ""}`);
  state.selectTab(want && want.getAttribute("role") === "tab" ? want.id : "tab-configure", "replace");
}

async function boot() {
  setupTabs();
  applyCloud();
  $("legend").replaceChildren(...LEGEND.map(([cls, label]) => h("span", { class: "lg" }, h("i", { class: `sw ${cls}` }), label)));
  let wired = false;
  window.addEventListener("hashchange", async () => {
    const t = takeTokenFromHash();
    if (!t) return;
    TOKEN = rememberToken(t);
    if (await connect() && CLOUD && !wired) wired = wire();
  });
  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = $("auth-input").value.trim();
    const m = raw.match(/(?:#|&|^)t=([A-Za-z0-9_-]+)/) || raw.match(/^([A-Za-z0-9_-]{20,})$/);
    $("auth-input").value = "";
    if (!m) return showError("That isn't the console URL or token. Copy the whole line that ends in #t=…");
    TOKEN = rememberToken(m[1]);
    if (await connect() && CLOUD && !wired) wired = wire();
  });
  if (await connect() && CLOUD) wired = wire();
}

// Populate the form and attach handlers, once, after the first successful connect.
function wire() {
  state.catalogue.scenarios = state.catalogue.scenarios.filter((s) => ofCloud(s.target_kind));
  const sel = $("scenario");
  sel.replaceChildren(...state.catalogue.scenarios.map((s) =>
    h("option", { value: s.name, text: scenarioLabel(s) })));
  for (const [t, only] of Object.entries(CLOUD_ONLY_FAULTS)) if (only !== CLOUD) delete state.catalogue.fault_types[t];
  $("add-fault-type").replaceChildren(...Object.entries(state.catalogue.fault_types).map(([t, d]) => h("option", { value: t, text: faultLabel(t), title: d })));
  sel.addEventListener("change", () => selectScenario(sel.value));
  $("add-fault").addEventListener("click", () => $("faults").append(faultRow({ type: $("add-fault-type").value })));
  $("reset").addEventListener("click", () => selectScenario(sel.value));
  $("sim-form").addEventListener("submit", runSimulation);
  $("ev-type").addEventListener("change", renderEvents);
  $("ev-search").addEventListener("input", renderEvents);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.result && renderTimeline($("timeline"), state.result), 150);
  });
  const initial = new URLSearchParams(location.search).get("scenario");
  if (initial && scenarioByName(initial)) sel.value = initial;
  selectScenario(sel.value);
  const runs = initRuns({ api, h, streamEvents, selectTab: state.selectTab, ofCloud }).catch((e) => showError(`Runs tab: ${e.message}`));
  initConfigure({ api, h, localPath, cloud: CLOUD, ofCloud, selectTab: state.selectTab, onRun: (cfg) => runs.then((r) => r && r.startRun(cfg)) })
    .catch((e) => showError(`Configure tab: ${e.message}`));
  return true;
}

if (!REDIRECTING) boot();
