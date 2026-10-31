// Simulator tab: rehearse a workload type's scenarios by hand (faults, load, overrides) against
// emulated cloud and JFrog endpoints, with the real client and rotator. Nothing in the cloud changes.

import { renderTimeline, hhmm, LEGEND } from "./timeline.js";
import { $, api, h, fill, streamEvents, CLOUD, ofCloud, statusPill, template, attachHelp } from "./core.js";

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
  federation_disabled: "AWS outbound federation off",
};
const CLOUD_ONLY_FAULTS = { arm_refresh_forbidden: ["azure"], sa_recreated: ["gcp"], rotator_down: ["gcp", "azure"],
  federation_disabled: ["aws"] };
const faultLabel = (t) => FAULT_LABELS[t] || t.replaceAll("_", " ");
const ACRONYMS = { jfrog: "JFrog", gcp: "GCP", aws: "AWS", sa: "SA", aca: "ACA", ecs: "ECS", arm: "ARM" };
const scenarioLabel = (s) => {
  const words = s.name.split("-").map((w) => ACRONYMS[w] || w).join(" ");
  return `${words[0].toUpperCase()}${words.slice(1)}`;
};
// The first choice simulates this workload's own saved configuration.
const OWN = "__config__";

const HELP = {
  "#scenario": "A recorded situation to rehearse, such as a token expiring or JFrog being down.",
  "#duration": "How much simulated time to run. The clock is simulated, so a day takes seconds.",
  "#seed": "Makes the random parts repeatable: the same seed gives the same run.",
};

const state = { catalogue: null, scenario: null, running: false, events: [], result: null, getConfig: null };

function showError(msg) {
  const e = $("form-error");
  e.textContent = msg || "";
  e.hidden = !msg;
}

function renderOverrides(s) {
  const box = $("overrides");
  box.replaceChildren();
  for (const [key, value] of Object.entries(s.settings || {})) {
    const numeric = typeof value === "number";
    const input = h("input", {
      id: `ov-${key}`, "data-key": key, "data-default": String(value), value: String(value),
      type: numeric ? "number" : "text", spellcheck: "false", ...(numeric ? { min: 0, step: 60 } : {}),
    });
    box.append(h("label", { class: "field" }, h("span", { text: SETTING_LABELS[key] || key }), input));
  }
  if (!box.children.length) box.append(h("p", { class: "muted small", text: "The configuration's own settings." }));
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
  const s = name === OWN ? { name: OWN, settings: {}, faults: [], description: "This workload's saved configuration" }
    : state.catalogue.scenarios.find((x) => x.name === name);
  state.scenario = s;
  const exp = s.expect?.verdict;
  $("scenario").title = s.description || "";
  fill($("scenario-meta"), exp ? [h("span", { class: "field-label", text: "Expected" }), verdictPill(exp)] : null);
  renderOverrides(s);
  $("faults").replaceChildren(...(s.faults || []).map(faultRow));
  renderLoad(s);
  const dur = $("duration");
  const want = s.duration || "24h";
  if (![...dur.options].some((o) => o.value === want)) dur.append(h("option", { value: want, text: want }));
  dur.value = want;
  $("seed").value = "1";
  showError("");
}

const VERDICT_STATUS = { pass: "succeeded", degraded: "warn", fail: "failed" };
const verdictPill = (v) => statusPill(VERDICT_STATUS[v] || "notrun", v);

function collectRequest() {
  const own = state.scenario.name === OWN;
  const body = { ...(own ? { config: state.getConfig() } : { scenario: state.scenario.name }), overrides: {}, load: {}, faults: [] };
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
    h("div", { class: "card-head" }, h("h2", { text: r.scenario }), verdictPill(r.verdict),
      h("span", { class: "muted small" }, `${Math.round(r.duration_s / 3600)} h · seed ${r.seed}`,
        expected ? h("span", { class: expected === r.verdict ? "match" : "mismatch",
          text: expected === r.verdict ? " · as expected" : ` · expected ${expected}` }) : null)),
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
  $("ev-count").textContent = `${rows.length} of ${state.events.length}` + (rows.length > shown.length ? ` (first ${shown.length} shown)` : "");
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

// kind: this workload's type; the scenario list shows only that type's scenarios.
export async function mountSim(mount, { kind, getConfig }) {
  state.getConfig = getConfig;
  mount.replaceChildren(template("tpl-sim"));
  attachHelp(mount, HELP);
  $("legend").replaceChildren(...LEGEND.map(([cls, label]) => h("span", { class: "lg" }, h("i", { class: `sw ${cls}` }), label)));
  try {
    state.catalogue = await api("/api/sim/scenarios");
  } catch (e) {
    showError(`Scenarios: ${e.message}`);
    $("run").disabled = true;
    return;
  }
  for (const [t, only] of Object.entries(CLOUD_ONLY_FAULTS)) if (!only.includes(CLOUD)) delete state.catalogue.fault_types[t];
  state.catalogue.scenarios = state.catalogue.scenarios.filter((s) => ofCloud(s.target_kind) && s.target_kind === kind);
  const sel = $("scenario");
  sel.replaceChildren(h("option", { value: OWN, text: "This workload's configuration" }),
    state.catalogue.scenarios.length ? h("optgroup", { label: "Scenarios" },
      state.catalogue.scenarios.map((s) => h("option", { value: s.name, text: scenarioLabel(s), title: s.description || "" }))) : null);
  const initial = new URLSearchParams(location.search).get("scenario");
  if (initial && state.catalogue.scenarios.some((s) => s.name === initial)) sel.value = initial;
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
    resizeTimer = setTimeout(() => state.result && !$("panel-sim")?.hidden && renderTimeline($("timeline"), state.result), 150);
  });
  selectScenario(sel.value);
}

// The timeline measures its container: draw it again once the tab is visible.
export function redrawSim() {
  if (state.result && $("timeline")) renderTimeline($("timeline"), state.result);
}
