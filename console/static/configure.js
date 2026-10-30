// Configure tab: edit one workload's configuration for the page's cloud, validate it
// live, Discover identity, Preflight, save, and start the pipeline.
// Rendered with DOM APIs only (no innerHTML); the config holds no secrets.

const $ = (id) => document.getElementById(id);

// replaceChildren() would turn null children into the text "null"; drop them.
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

let api, h, cloud, ofCloud, onRun, selectTab;
// showAll: list problems on empty fields too. Off for a new configuration until a button is pressed.
const st = { inv: null, schema: null, cfg: null, dirty: false, busy: false, validateTimer: null, validation: null, showAll: false, saved: null, current: null };

// ----------------------------------------------------------------- path helpers

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

function setPath(obj, path, value) {
  const keys = path.split(".");
  let cur = obj;
  for (const k of keys.slice(0, -1)) {
    if (!cur[k] || typeof cur[k] !== "object") cur[k] = {};
    cur = cur[k];
  }
  cur[keys[keys.length - 1]] = value;
}

const clone = (o) => JSON.parse(JSON.stringify(o));

function mergeDeep(base, over) {
  for (const [k, v] of Object.entries(over || {})) {
    if (v && typeof v === "object" && !Array.isArray(v) && base[k] && typeof base[k] === "object") mergeDeep(base[k], v);
    else base[k] = v;
  }
  return base;
}

// A new configuration starts with the server's defaults minus the sample names and URL,
// so nothing on screen looks filled in when it isn't.
function freshConfig(kind) {
  const cfg = clone(st.schema.defaults[kind]);
  cfg.name = "";  // set from the picked workload or the Name typed below
  for (const f of cloud === "gcp" ? ["project", "name"] : ["subscription", "resource_group", "name"]) cfg.target[cloud][f] = "";
  cfg.jfrog.url = "";
  // What the logged-in account and the pipelines already use.
  const d = st.inv?.defaults || {};
  if (cloud === "gcp") {
    if (d.project) cfg.target.gcp.project = d.project;
    if (d.region) cfg.target.gcp.region = d.region;
  } else if (d.subscription) cfg.target.azure.subscription = d.subscription;
  if (d.jfrog_url) cfg.jfrog.url = d.jfrog_url;
  // Images come from Artifact Registry / ACR with the platform's own identity; JFrog
  // serves artifacts to the app's code. The JFrog image-pull path is opt-in.
  cfg.pull_path.enabled = false;
  return cfg;
}

function cloudKinds() {
  return Object.keys(st.schema.kinds).filter((k) => st.schema.kinds[k].cloud === cloud);
}

// ----------------------------------------------------------------- form <-> config

function fields() {
  return [...$("cfg-form").querySelectorAll("[data-path]")];
}

function writeForm() {
  for (const el of fields()) {
    const v = getPath(st.cfg, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v === null || v === undefined ? "" : String(v);
  }
  applyKindVisibility();
  renderDiscovered();
  renderSummary();
}

function readField(el) {
  if (el.type === "checkbox") return el.checked;
  const raw = el.value.trim();
  if (el.dataset.type === "number") return raw === "" ? 0 : Number(raw);
  if (el.dataset.type === "nullable") return raw === "" ? null : raw;
  return raw;
}

function onFieldInput(ev) {
  const el = ev.target.closest("[data-path]");
  if (!el) return;
  // The configuration name follows the workload name until it is edited on its own.
  const nameEl = $("cfg-form").querySelector('[data-path="name"]');
  if (el.dataset.path === `target.${cloud}.name` && (!st.cfg.name || st.cfg.name === st.cfg.target[cloud].name)) {
    st.cfg.name = nameEl.value = readField(el);
  }
  setPath(st.cfg, el.dataset.path, readField(el));
  if (el.dataset.path === "target.kind" || el.dataset.path === "pull_path.enabled") applyKindVisibility();
  markDirty(true);
  scheduleValidate();
  renderSummary();
}

// Flex Consumption runs code packages and never pulls an image, so it has no image choice.
function applyKindVisibility() {
  const kind = st.cfg.target.kind;
  for (const el of $("cfg-form").querySelectorAll("[data-kinds]")) el.hidden = !el.dataset.kinds.split(" ").includes(kind);
  const pullChoice = $("cfg-form").querySelector('[data-path="pull_path.enabled"]').closest("label");
  pullChoice.hidden = kind === "azure_functions_flex";
  $("cfg-pull-fields").hidden = pullChoice.hidden || !st.cfg.pull_path?.enabled;
  renderDiscovered();
}

function markDirty(on) {
  st.dirty = on;
  const dirtyEl = $("cfg-dirty");
  dirtyEl.textContent = on ? "unsaved changes" : st.current ? "saved" : "";
  dirtyEl.className = on ? "pill warn" : "pill ok";
  dirtyEl.hidden = !dirtyEl.textContent;
  if (st.cfg) renderTitle();
  if (st.saved) renderList();
}

// The field's own label, so problems read "App name is required", not "target.azure.name: …".
function labelFor(path) {
  const el = $("cfg-form").querySelector(`[data-path="${CSS.escape(path)}"]`);
  const text = el?.closest("label")?.querySelector("span, b")?.textContent?.trim();
  return { el, text: text || path };
}

// ----------------------------------------------------------------- summary panel

function renderSummary() {
  const c = st.cfg;
  if (!c) return;
  const d = c.target?.discovered;
  const found = !!d && (!d.kind || d.kind === c.target.kind);
  const urlOk = /^https:\/\/[a-z0-9.-]+\/?$/i.test(c.jfrog?.url || "");
  $("step-app").classList.toggle("is-done", found);
  $("step-jfrog").classList.toggle("is-done", urlOk && !!c.jfrog?.reader_group);
  $("step-needs").classList.toggle("is-done", !!(c.runtime?.enabled || c.pull_path?.enabled));
  renderEssentials(c, d, found);
}

// The side panel shows only what the form doesn't: the identity Discover found.
function renderEssentials(c, d, found) {
  const identity = d?.service_account || d?.principal_id || d?.client_id || "";
  fill($("cfg-essentials"), h("dt", { text: "Identity" }), h("dd", {},
    identity ? h("span", { class: "with-icon" }, statusIcon(found ? "ok" : "warn"), h("span", { class: "mono", text: identity }))
      : h("span", { class: "with-icon muted" }, statusIcon("skip"), "not discovered")));
}

const STATUS_GLYPH = { ok: "✓", warn: "!", fail: "✕", error: "✕", warning: "!", skip: "–" };
function statusIcon(status) {
  return h("span", { class: `st-dot st-dot-${status}`, "aria-hidden": "true", text: STATUS_GLYPH[status] || "?" });
}

// ----------------------------------------------------------------- validation

function scheduleValidate() {
  clearTimeout(st.validateTimer);
  st.validateTimer = setTimeout(validate, 250);
}

// Run pipeline needs a workload; until one is picked it is off with a quiet hint, not errors.
function gateRun() {
  const ready = !!st.cfg?.target?.[cloud]?.name;
  $("cfg-run-btn").disabled = !ready || st.busy;
  if (!ready && !st.busy) status("Pick a workload to run the pipeline.", "idle");
  else if (ready && $("cfg-status").textContent === "Pick a workload to run the pipeline.") status("", "idle");
}

async function validate() {
  let res;
  try {
    res = await api("/api/validate", { method: "POST", body: { config: st.cfg } });
  } catch {
    return;
  }
  st.validation = res;
  gateRun();
  const shown = (p) => {
    if (st.showAll) return true;
    const el = $("cfg-form").querySelector(`[data-path="${CSS.escape(p.path)}"]`);
    return !!el && el.type !== "checkbox" && el.value.trim() !== "";
  };
  for (const el of fields()) {
    el.removeAttribute("aria-invalid");
    el.closest("label")?.querySelector(".field-msg")?.remove();
  }
  const byPath = new Map();
  for (const item of [...res.errors.map((e) => ({ ...e, level: "error" })), ...res.warnings.map((w) => ({ ...w, level: "warning" }))]) {
    if (!byPath.has(item.path)) byPath.set(item.path, item);
  }
  for (const [path, item] of byPath) {
    if (!shown(item)) continue;
    const el = $("cfg-form").querySelector(`[data-path="${CSS.escape(path)}"]`);
    if (!el || el.closest("[hidden]")) continue;
    if (item.level === "error") el.setAttribute("aria-invalid", "true");
    el.closest("label")?.append(h("span", { class: `field-msg ${item.level}`, text: item.message }));
  }
  const problems = [...res.errors.map((e) => ({ ...e, level: "error" })), ...res.warnings.map((w) => ({ ...w, level: "warning" }))]
    .filter(shown);
  const items = problems.map((p) => h("li", { class: p.level }, statusIcon(p.level),
    h("div", {}, h("b", { text: labelFor(p.path).text }), h("div", { class: "issue-msg", text: p.message }))));
  $("cfg-problems").hidden = !items.length;
  const nErr = problems.filter((p) => p.level === "error").length;
  fill($("cfg-problems"), items.length ? h("div", { class: "panel-head" },
    h("h3", { text: res.errors.length ? "Fix before running the pipeline" : "Warnings" }),
    h("span", { class: `pill ${nErr ? "bad" : "warn"}`, text: String(items.length) })) : null,
    items.length ? h("ul", { class: "issues" }, items) : null);
}

// ----------------------------------------------------------------- saved configs

// The Configurations page lists what is on disk. The configuration open in the Configure tab is
// marked, and its row shows the live workload type with an "unsaved changes" tag while edited,
// so the list never contradicts the form.
async function refreshSaved() {
  const { configs } = await api("/api/configs");
  st.saved = configs.filter((c) => ofCloud(c.kind));
  renderList();
}

const kindLabel = (k) => st.schema.kinds[k]?.label || k || "";

function renderList() {
  const rows = st.saved.map((c) => {
    const open = c.name === st.current;
    const kind = open ? st.cfg.target.kind : c.kind;
    return h("tr", { class: open ? "is-open" : "" },
      h("th", { scope: "row" }, h("button", { type: "button", class: "link cfg-open", text: c.name, "data-open": c.name }),
        open ? h("span", { class: "tag", text: st.dirty ? "open · unsaved changes" : "open" }) : null),
      h("td", { text: kindLabel(kind) + (open && kind !== c.kind ? ` (saved as ${kindLabel(c.kind)})` : "") }),
      h("td", { class: "mono small", text: c.workload || "—" }),
      h("td", {}, c.identity ? h("span", { class: "with-icon" }, statusIcon("ok"), h("span", { class: "mono small", text: c.identity }))
        : h("span", { class: "with-icon muted small" }, statusIcon("skip"), "not discovered")),
      h("td", { class: "small muted", text: (c.updated || "").replace("T", " ").replace("Z", " UTC") }),
      h("td", { class: "actions" }, h("button", { type: "button", class: "secondary small-btn", text: "Open", "data-open": c.name }),
        h("button", { type: "button", class: "link danger", text: "Delete", "data-delete": c.name })));
  });
  fill($("cfg-saved"), rows);
  $("cfg-empty").hidden = st.saved.length > 0;
  $("cfg-saved").closest("table").hidden = st.saved.length === 0;
  // Nothing saved for this cloud: no Configurations tab and no link back to it.
  const none = st.saved.length === 0;
  $("tab-configs").hidden = none;
  $("cfg-back").closest(".cfg-crumb").hidden = none;
  if (none && $("tab-configs").getAttribute("aria-selected") === "true") selectTab("tab-configure");
}

function renderTitle() {
  const c = st.cfg;
  $("cfg-title").textContent = st.current ? `${c.name || st.current} · ${kindLabel(c.target.kind)}`
    : `New configuration · ${kindLabel(c.target.kind)}`;
}

function resetPanels() {
  $("cfg-discover").hidden = true;
  $("cfg-preflight").hidden = true;
}

function newConfig(kind) {
  if (st.cfg && st.dirty && !armed($("cfg-new"), "Discard changes?", "New configuration")) return;
  st.cfg = freshConfig(kind);
  st.current = null;
  st.showAll = false;
  resetPanels();
  writeForm();
  markDirty(true);
  scheduleValidate();
}

async function loadConfig(name, btn) {
  if (!name) return;
  if (st.current === name) return selectTab("tab-configure");
  if (st.dirty && btn && !armed(btn, "Discard unsaved changes?", btn.textContent)) return;
  await busy("Loading…", async () => {
    const { config } = await api(`/api/configs/${encodeURIComponent(name)}`);
    const kind = cloudKinds().includes(config?.target?.kind) ? config.target.kind : cloudKinds()[0];
    st.cfg = mergeDeep(clone(st.schema.defaults[kind]), config);
    st.current = name;
    st.showAll = true;
    selectTab("tab-configure");
    resetPanels();
    writeForm();
    markDirty(false);
    status(`Loaded ${name}`, "ok");
  });
}

// Two-step confirmation on the button itself: native confirm() dialogs block and are easy to dismiss.
function armed(btn, prompt, label) {
  if (btn.dataset.armed === "1") {
    btn.dataset.armed = "";
    btn.textContent = label;
    return true;
  }
  btn.dataset.armed = "1";
  btn.textContent = prompt;
  setTimeout(() => { btn.dataset.armed = ""; btn.textContent = label; }, 4000);
  return false;
}

async function save() {
  st.showAll = true;
  const name = st.cfg.name;
  await busy("Saving…", async () => {
    await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: st.cfg } });
    st.current = name;
    markDirty(false);
    await refreshSaved();
    renderTitle();
    status(`Saved configuration ${name}`, "ok");
  });
}

async function remove(name, btn) {
  if (!name) return;
  if (!armed(btn, `Delete ${name} and its runs?`, "Delete")) return;
  await busy("Deleting…", async () => {
    const res = await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (st.current === name) {
      st.dirty = false;
      newConfig(st.cfg.target.kind);
      markDirty(false);
    }
    await refreshSaved();
    const kept = res.runs_kept ? `; ${res.runs_kept} active run(s) kept until they finish` : "";
    status(`Deleted ${name} and ${res.runs_deleted} run(s)${kept}`, "ok");
  });
}

// ----------------------------------------------------------------- busy / status

async function busy(label, fn) {
  if (st.busy) return;
  st.busy = true;
  const buttons = [...document.querySelectorAll("#cfg-actions button, #panel-configs button, #cfg-discover-btn")];
  buttons.forEach((b) => { b.disabled = true; });
  status(label, "busy");
  try {
    await fn();
  } catch (e) {
    status(e.message || String(e), "error");
  } finally {
    st.busy = false;
    buttons.forEach((b) => { b.disabled = false; });
    scheduleValidate();
  }
}

function status(text, kind) {
  const el = $("cfg-status");
  el.textContent = text;
  el.className = `small status-${kind}`;
}

function issueList(items, cls, icon) {
  return items.length ? h("ul", { class: "issues" }, items.map((i) => h("li", { class: cls }, statusIcon(cls),
    h("div", {}, h("div", { class: "issue-msg", text: i.message }), i.fix ? h("div", { class: "fix small", text: `Fix: ${i.fix}` }) : null)))) : null;
}

function badge(status) {
  return h("span", { class: `pill ${status === "ok" ? "ok" : status === "warn" ? "warn" : "bad"}`, text: status });
}

function modeNote(res) {
  if (res.mode !== "replay") return null;
  return h("p", { class: "small warn-text", text: "Fixture replay: these results come from recorded fixtures, not your accounts." +
    (res.fixture_misses?.length ? ` ${res.fixture_misses.length} call(s) had no fixture.` : "") });
}

// ----------------------------------------------------------------- discover

async function runDiscover() {
  await busy("Discovering identity… read-only gcloud/az calls, about 30 s", async () => {
    const res = await api("/api/discover", { method: "POST", body: { config: st.cfg } });
    for (const [path, value] of Object.entries(res.config_patch || {})) setPath(st.cfg, path, value);
    renderSummary();
    if (Object.keys(res.config_patch || {}).length) markDirty(true);
    writeForm();
    const box = $("cfg-discover");
    box.hidden = false;
    fill(box, 
      h("div", { class: "panel-head" }, h("h3", { text: "Discover" }), badge(res.status)),
      modeNote(res),
      res.corrections.length ? h("div", { class: "corrections" }, h("b", { text: "Corrected to match what the cloud reports:" }),
        h("ul", {}, res.corrections.map((c) => h("li", {}, h("code", { text: c.field }), ` ${String(c.from)} → `,
          h("b", { text: String(c.to) }), h("div", { class: "small muted", text: c.reason }))))) : null,
      issueList(res.errors, "error", "✗"),
      issueList(res.warnings, "warning", "⚠"),
      null);
    status(res.status === "fail" ? "Discovery failed: see the errors above" : "Identity discovered",
      res.status === "fail" ? "error" : "ok");
  });
}

function renderDiscovered() {
  const box = $("cfg-discovered");
  const d = st.cfg?.target?.discovered;
  if (!d) {
    box.replaceChildren();
    return;
  }
  const stale = d.kind && d.kind !== st.cfg.target.kind;
  fill(box, stale ? h("p", { class: "small warn-text", text: "Run Discover identity again." }) : null);
}

// ----------------------------------------------------------------- preflight

async function runPreflight() {
  let res = null;
  await busy("Running preflight… read-only, up to a minute", async () => {
    res = await api("/api/preflight", { method: "POST", body: { config: st.cfg } });
    for (const [path, value] of Object.entries(res.config_patch || {})) setPath(st.cfg, path, value);
    renderSummary();
    const icon = STATUS_GLYPH;
    const box = $("cfg-preflight");
    box.hidden = false;
    fill(box, 
      h("div", { class: "panel-head" }, h("h3", { text: "Preflight" }), badge(res.status)),
      h("div", { class: "counts" },
        h("span", { class: "with-icon" }, statusIcon("ok"), `${res.counts.ok} ok`),
        h("span", { class: "with-icon" }, statusIcon("warn"), `${res.counts.warn} warn`),
        h("span", { class: "with-icon" }, statusIcon("fail"), `${res.counts.fail} fail`)),
      modeNote(res),
      h("ul", { class: "checklist" }, res.checks.map((c) => h("li", { class: `chk chk-${c.status}` },
        h("span", { class: `chk-icon st-dot st-dot-${c.status}`, "aria-label": c.status, text: icon[c.status] || "?" }),
        h("div", {}, h("b", { text: c.title }), c.detail && c.status !== "ok" ? h("div", { class: "small mono", text: c.detail }) : null,
          c.fix && c.status !== "ok" ? h("div", { class: "fix small", text: `Fix: ${c.fix}` }) : null)))));
    status(res.status === "fail" ? "Preflight failed: see the checks" : "Preflight passed",
      res.status === "fail" ? "error" : "ok");
  });
  return res;
}

// ----------------------------------------------------------------- init

// ----------------------------------------------------------------- inventory

async function loadInventory(scope = "", refresh = false) {
  const sel = $("cfg-workload");
  fill(sel, h("option", { value: "", text: "Loading workloads…" }));
  const q = new URLSearchParams({ ...(scope && { scope }), ...(refresh && { refresh: "1" }) }).toString();
  try {
    st.inv = await api(`/api/inventory/${cloud}${q ? `?${q}` : ""}`);
  } catch (e) {
    fill(sel, h("option", { value: "", text: `Could not list workloads: ${e.message}` }));
    return;
  }
  // Listings are cached server side; the time says how old this one is.
  $("cfg-inv-refresh").title = st.inv.fetched_at
    ? `Listed ${new Date(st.inv.fetched_at * 1000).toLocaleString()} · fetch again` : "Fetch again";
  // Every project / subscription the login can see: existing apps live in any of them.
  const current = cloud === "gcp" ? st.inv.defaults.project : st.inv.defaults.subscription;
  $("cfg-scope-label").textContent = cloud === "gcp" ? "Project" : "Subscription";
  const scopes = st.inv.scopes?.length ? st.inv.scopes : current ? [{ id: current, name: current }] : [];
  // Names only: the ID shows in the Project ID / Subscription ID field below. A repeated name keeps its ID.
  const seen = {};
  scopes.forEach((s) => { seen[s.name] = (seen[s.name] || 0) + 1; });
  fill($("cfg-scope"), scopes.map((s) => h("option", { value: s.id, selected: s.id === current, title: s.id,
    text: !s.name || s.name === s.id ? s.id : seen[s.name] > 1 ? `${s.name} (${s.id})` : s.name })));
  if (!st.current && !st.dirty) {
    st.cfg = freshConfig(st.cfg.target.kind);
    writeForm();
  }
  const groups = {};
  st.inv.workloads.forEach((w, i) => {
    const label = st.schema.kinds[w.kind]?.label || "Container Apps job";
    (groups[label] ||= []).push(h("option", { value: String(i), disabled: !w.supported,
      text: `${w.name} · ${w.resource_group ? `${w.resource_group} · ` : ""}${w.region}${w.onboarded ? " · onboarded" : ""}${w.supported ? "" : " · not supported"}` }));
  });
  const n = st.inv.workloads.length;
  const fresh = st.inv.workloads.filter((w) => !w.onboarded).length;
  fill(sel, h("option", { value: "", text: n ? `${n} workload${n === 1 ? "" : "s"} · ${fresh} not onboarded` : st.inv.errors[0] || "No workloads found" }),
    Object.entries(groups).map(([label, opts]) => h("optgroup", { label }, opts)));
}

function pickWorkload() {
  const w = st.inv?.workloads[Number($("cfg-workload").value)];
  if (!w || !$("cfg-workload").value) return;
  st.cfg = freshConfig(w.kind);
  st.cfg.name = w.name;
  st.cfg.target[cloud].name = w.name;
  if (cloud === "gcp") Object.assign(st.cfg.target.gcp, { project: w.project, region: w.region });
  else Object.assign(st.cfg.target.azure, { subscription: w.subscription, resource_group: w.resource_group });
  st.current = null;
  resetPanels();
  writeForm();
  markDirty(true);
  scheduleValidate();
  runDiscover();
}

export async function initConfigure(deps) {
  ({ api, h, cloud, ofCloud, onRun, selectTab } = deps);
  st.schema = await api("/api/schema");
  $("cfg-kind").replaceChildren(...cloudKinds().map((k) => h("option", { value: k, text: st.schema.kinds[k].label })));

  $("cfg-form").addEventListener("input", onFieldInput);
  $("cfg-form").addEventListener("change", onFieldInput);
  $("cfg-form").addEventListener("submit", (e) => e.preventDefault());
  $("cfg-saved").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b?.dataset.open) loadConfig(b.dataset.open, b);
    else if (b?.dataset.delete) remove(b.dataset.delete, b);
  });
  $("cfg-new").addEventListener("click", () => {
    if (st.cfg && st.dirty && !armed($("cfg-new"), "Discard unsaved changes?", "New configuration")) return;
    st.dirty = false;
    newConfig(cloudKinds()[0]);
    selectTab("tab-configure");
  });
  $("cfg-back").addEventListener("click", () => selectTab("tab-configs"));
  $("cfg-save").addEventListener("click", save);
  $("cfg-discover-btn").addEventListener("click", runDiscover);
  $("cfg-workload").addEventListener("change", pickWorkload);
  $("cfg-scope").addEventListener("change", () => loadInventory($("cfg-scope").value));
  $("cfg-inv-refresh").addEventListener("click", (e) => { e.preventDefault(); loadInventory($("cfg-scope").value, true); });
  $("cfg-preflight-btn").addEventListener("click", runPreflight);
  $("cfg-run-btn").addEventListener("click", async () => {
    st.showAll = true;
    await validate();
    if (st.validation?.errors?.length) return status("Fix the validation errors first.", "error");
    // Missing APIs, providers, roles or pipeline code stop here, not halfway through the run.
    const pre = await runPreflight();
    if (!pre || pre.status === "fail") return status("Preflight failed: fix the checks, then Run pipeline again.", "error");
    onRun(clone(st.cfg));
  });

  st.cfg = freshConfig(cloudKinds()[0]);
  await refreshSaved();
  writeForm();
  markDirty(false);
  scheduleValidate();
  // The path picks the tab; with none (or Configurations and nothing saved), land on the useful one.
  const named = location.pathname.split("/")[2] || "";
  if (!named || (named === "configs" && !st.saved.length)) selectTab(st.saved.length ? "tab-configs" : "tab-configure", "replace");
  loadInventory();
}
