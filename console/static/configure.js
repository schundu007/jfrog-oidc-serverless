// The configuration form, used by New workload and by a workload's Configuration tab: edit one
// workload's configuration for the page's cloud, validate it live, Discover identity, Preflight
// (results in the side panel) and Save. Step cards are always open.
// Rendered with DOM APIs only (no innerHTML); the config holds no secrets.

import { $, api, h, fill, streamEvents, CLOUD as cloud, statusIcon, statusPill, template, attachHelp,
  flyout, status as setStatus } from "./core.js";

// showAll: list problems on empty fields too. Off for a new configuration until a button is pressed.
const st = { inv: null, schema: null, cfg: null, dirty: false, busy: false, validateTimer: null, validation: null,
  showAll: false, mode: "new", name: null, onChange: () => {} };

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
const SCOPE_KEY = { gcp: "project", azure: "subscription", aws: "profile", openshift: "context" };
const SCOPE_LABEL = { gcp: "Project", azure: "Subscription", aws: "AWS profile", openshift: "OpenShift cluster" };
export const SCOPE_FIELDS = { gcp: ["project", "region"], azure: ["subscription", "resource_group"],
  aws: ["account", "region", "profile", "cluster"], openshift: ["context", "cluster", "namespace"] };

function freshConfig(kind) {
  const cfg = clone(st.schema.defaults[kind]);
  cfg.name = "";  // set from the picked workload or the Name typed below
  // Every target field starts empty: no sample values.
  for (const f of Object.keys(cfg.target[cloud])) if (typeof cfg.target[cloud][f] === "string" && f !== "identity_type") cfg.target[cloud][f] = "";
  cfg.jfrog.url = "";
  // What the logged-in account and the pipelines already use, only when the listing worked.
  const inv = st.inv;
  const d = inv && (!inv.errors?.length || inv.workloads?.length) ? inv.defaults || {} : {};
  for (const [k, v] of Object.entries(d)) if (v && k !== "name" && Object.hasOwn(cfg.target[cloud], k)) cfg.target[cloud][k] = v;
  if (inv?.defaults?.jfrog_url) cfg.jfrog.url = inv.defaults.jfrog_url;
  // Images come from Artifact Registry / ACR with the platform's own identity; JFrog
  // serves artifacts to the app's code. The JFrog image-pull path is opt-in.
  cfg.pull_path.enabled = false;
  return cfg;
}

function cloudKinds() {
  return Object.keys(st.schema.kinds).filter((k) => st.schema.kinds[k].cloud === cloud);
}

export const kindLabel = (schema, k) => schema?.kinds?.[k]?.label || k || "";

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
  // The configuration name follows the workload name until it is edited on its own (new workloads only).
  const nameEl = $("cfg-form").querySelector('[data-path="name"]');
  if (st.mode === "new" && el.dataset.path === `target.${cloud}.name` && (!st.cfg.name || st.cfg.name === st.cfg.target[cloud].name)) {
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
  // Flex has no image; AWS and Knative pull from their own registry (runtime path only).
  pullChoice.hidden = kind === "azure_functions_flex" || !st.schema.pull_clouds.includes(cloud);
  $("cfg-pull-fields").hidden = pullChoice.hidden || !st.cfg.pull_path?.enabled;
  renderSummary();
}

function markDirty(on) {
  st.dirty = on;
  const el = $("cfg-dirty");
  if (el) {
    el.textContent = on ? "Unsaved changes" : "";
    el.className = on ? "pill pill-warn" : "";
    el.hidden = !on;
  }
}

// The field's own label, so problems read "App name is required", not "target.azure.name: …".
function labelFor(path) {
  const el = $("cfg-form").querySelector(`[data-path="${CSS.escape(path)}"]`);
  const label = el?.closest("label")?.querySelector("span, b")?.cloneNode(true);
  label?.querySelectorAll(".help, button").forEach((n) => n.remove());
  const text = label?.textContent?.trim();
  return { el, text: text || path };
}

// ----------------------------------------------------------------- identity and step marks

export function identityOf(cfg) {
  const d = cfg?.target?.discovered;
  const found = !!d && (!d.kind || d.kind === cfg.target.kind);
  const id = d?.service_account || d?.principal_id || d?.client_id || "";
  // A dedicated identity the run creates has no ID yet: show its name.
  return { found, text: id || (d?.dedicated_identity ? `${d.dedicated_identity} (new)` : ""), stale: !!d && !found };
}

function renderSummary() {
  const c = st.cfg;
  if (!c) return;
  const idn = identityOf(c);
  const urlOk = /^https:\/\/[a-z0-9.-]+\/?$/i.test(c.jfrog?.url || "");
  $("step-app").classList.toggle("is-done", idn.found);
  $("step-jfrog").classList.toggle("is-done", urlOk && !!c.jfrog?.reader_group);
  $("step-needs").classList.toggle("is-done", !!(c.runtime?.enabled || c.pull_path?.enabled));
  fill($("cfg-identity"), idn.text
    ? h("span", { class: "with-icon" }, statusIcon(idn.found ? "succeeded" : "warn", idn.found ? "discovered" : "stale"),
      h("span", { class: "mono", text: idn.text }), idn.stale ? h("span", { class: "muted", text: "· run Discover identity again" }) : null)
    : h("span", { class: "with-icon muted" }, statusIcon("notrun", "not discovered"), "Identity not discovered"));
  st.onChange(c);
}

// ----------------------------------------------------------------- validation

function scheduleValidate() {
  clearTimeout(st.validateTimer);
  st.validateTimer = setTimeout(validate, 250);
}

async function validate() {
  let res;
  try {
    res = await api("/api/validate", { method: "POST", body: { config: st.cfg } });
  } catch {
    return;
  }
  st.validation = res;
  const shown = (p) => {
    if (st.showAll) return true;
    const el = $("cfg-form").querySelector(`[data-path="${CSS.escape(p.path)}"]`);
    return !!el && el.type !== "checkbox" && el.value.trim() !== "";
  };
  for (const el of fields()) {
    el.removeAttribute("aria-invalid");
    el.closest("label")?.querySelector(".field-msg")?.remove();
  }
  const all = [...res.errors.map((e) => ({ ...e, level: "error" })), ...res.warnings.map((w) => ({ ...w, level: "warning" }))];
  const byPath = new Map();
  for (const item of all) if (!byPath.has(item.path)) byPath.set(item.path, item);
  for (const [path, item] of byPath) {
    if (!shown(item)) continue;
    const el = $("cfg-form").querySelector(`[data-path="${CSS.escape(path)}"]`);
    // A field hidden by the workload type or a switched-off path gets no message; a hidden tab still does.
    if (!el || el.closest("[hidden]:not([role=tabpanel])")) continue;
    if (item.level === "error") el.setAttribute("aria-invalid", "true");
    el.closest("label")?.append(h("span", { class: `field-msg ${item.level}`, text: item.message }));
  }
  const problems = all.filter(shown);
  const items = problems.map((p) => h("li", {}, statusIcon(p.level),
    h("div", {}, h("b", { text: labelFor(p.path).text }), h("div", { class: "issue-msg", text: p.message }))));
  const box = $("cfg-problems");
  box.hidden = !items.length;
  fill(box, items.length ? h("div", { class: "card-head" },
    h("h2", { text: res.errors.length ? "Fix before running the pipeline" : "Warnings" }),
    h("span", { class: "count-badge", text: String(items.length) })) : null,
  items.length ? h("ul", { class: "issues" }, items) : null);
}

export async function validateNow() {
  st.showAll = true;
  await validate();
  return st.validation;
}

// ----------------------------------------------------------------- save

async function save() {
  st.showAll = true;
  const name = st.cfg.name;
  if (!name) {
    await validate();
    return status("Name the configuration first (Target workload → Configuration name).", "error");
  }
  let ok = false;
  await busy("Saving…", async () => {
    // A new workload never overwrites an existing one with the same name.
    if (st.mode === "new") {
      const { configs } = await api("/api/configs");
      if (configs.some((c) => c.name === name)) throw new Error(`A workload named ${name} exists. Pick another name.`);
    }
    await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: st.cfg } });
    markDirty(false);
    status(`Saved ${name}`, "ok");
    ok = true;
  });
  if (ok) st.onSaved?.(name);
}

// ----------------------------------------------------------------- busy / status

async function busy(label, fn) {
  if (st.busy) return false;
  st.busy = true;
  const buttons = [...document.querySelectorAll("[data-lock]")];
  buttons.forEach((b) => { b.disabled = true; });
  status(label, "busy");
  let ok = true;
  try {
    await fn();
  } catch (e) {
    ok = false;
    status(e.message || String(e), "error");
  } finally {
    st.busy = false;
    buttons.forEach((b) => { b.disabled = false; });
    scheduleValidate();
  }
  return ok;
}

export function status(text, kind) {
  setStatus($("cfg-status"), text, kind);
}

function issueList(items, level) {
  return items.length ? h("ul", { class: "issues" }, items.map((i) => h("li", {}, statusIcon(level),
    h("div", {}, h("div", { class: "issue-msg", text: i.message }), i.fix ? h("div", { class: "fix small", text: `Fix: ${i.fix}` }) : null)))) : null;
}

function modeNote(res) {
  if (res.mode !== "replay") return null;
  return res.fixture_misses?.length ? h("p", { class: "small warn-text", text: `Replay: ${res.fixture_misses.length} call(s) had no fixture.` }) : null;
}

// ----------------------------------------------------------------- discover

async function runDiscover() {
  await busy("Discovering identity… read-only gcloud/az calls, about 30 s", async () => {
    const res = await api("/api/discover", { method: "POST", body: { config: st.cfg } });
    for (const [path, value] of Object.entries(res.config_patch || {})) setPath(st.cfg, path, value);
    if (Object.keys(res.config_patch || {}).length) markDirty(true);
    writeForm();
    const box = $("cfg-discover");
    const extra = [modeNote(res),
      res.corrections.length ? h("div", { class: "corrections" }, h("b", { text: "Corrected to match what the cloud reports:" }),
        h("ul", {}, res.corrections.map((c) => h("li", {}, h("code", { text: c.field }), ` ${String(c.from)} → `,
          h("b", { text: String(c.to) }), h("div", { class: "small muted", text: c.reason }))))) : null,
      issueList(res.errors, "error"),
      issueList(res.warnings, "warning")].filter(Boolean);
    // A clean discovery shows only the identity next to the button.
    box.hidden = !extra.length;
    fill(box, extra.length ? h("div", { class: "card-head" }, h("h3", { text: "Discover" }), statusPill(res.status)) : null, extra);
    status(res.status === "fail" ? "Discovery failed: see the errors under Discover identity" : "Identity discovered",
      res.status === "fail" ? "error" : "ok");
  });
}

// ----------------------------------------------------------------- preflight (side panel)

function checkItem(c) {
  return h("li", { class: `chk chk-${c.status}` }, statusIcon(c.status),
    h("div", {}, h("b", { text: c.title }), c.detail && c.status !== "ok" ? h("div", { class: "small mono", text: c.detail }) : null,
      c.fix && c.status !== "ok" ? h("div", { class: "fix small", text: `Fix: ${c.fix}` }) : null));
}

// Preflight streams into the side panel: each check and each command line as it happens, then the summary.
export async function runPreflight() {
  let res = null;
  await busy("Running preflight… read-only, up to a minute", async () => {
    const panel = flyout("Preflight");
    const list = h("ul", { class: "checklist" });
    const log = h("pre", { class: "pf-log" });
    const logBox = h("details", { class: "pf-log-box", open: true }, h("summary", { text: "Log" }), log);
    fill(panel.status, statusPill("running", "running"));
    fill(panel.body, h("p", { class: "muted small", text: "Read-only checks." }), list, logBox);
    let failed = null;
    await streamEvents("/api/preflight?stream=1", (kind, data) => {
      if (kind === "check") list.append(checkItem(data));
      else if (kind === "log") { log.append(`${data.line}\n`); log.scrollTop = log.scrollHeight; }
      else if (kind === "done") res = data;
      else if (kind === "failed") failed = data.error;
    }, undefined, { method: "POST", body: { config: st.cfg } });
    if (failed) { fill(panel.status, statusPill("failed")); throw new Error(`Preflight failed: ${failed}`); }
    if (!res) { fill(panel.status, statusPill("failed")); throw new Error("Preflight stopped before it finished."); }
    for (const [path, value] of Object.entries(res.config_patch || {})) setPath(st.cfg, path, value);
    renderSummary();
    logBox.open = false;
    fill(panel.status, statusPill(res.status, res.status === "ok" ? "passed" : res.status === "warn" ? "passed with warnings" : "failed"),
      h("span", { class: "muted small", text: `${res.counts.ok} ok · ${res.counts.warn} warn · ${res.counts.fail} fail` }));
    fill(panel.body, modeNote(res), h("ul", { class: "checklist" }, res.checks.map(checkItem)), logBox);
    status(res.status === "fail" ? "Preflight failed: see the checks" : "Preflight passed", res.status === "fail" ? "error" : "ok");
  });
  return res;
}

// ----------------------------------------------------------------- inventory (New workload)

async function loadInventory(scope = "", refresh = false) {
  const sel = $("cfg-workload");
  fill(sel, h("option", { value: "", text: "Loading workloads…" }));
  const q = new URLSearchParams({ ...(scope && { scope }), ...(refresh && { refresh: "1" }) }).toString();
  try {
    st.inv = await api(`/api/inventory/${cloud}${q ? `?${q}` : ""}`);
  } catch (e) {
    fill(sel, h("option", { value: "", text: `Could not list workloads: ${e.message}` }));
    $("cfg-scope-label").textContent = SCOPE_LABEL[cloud];
    fill($("cfg-scope"), h("option", { value: "", text: "" }));
    return;
  }
  // Listings are cached server side; the time says how old this one is.
  $("cfg-inv-refresh").title = st.inv.fetched_at
    ? `Listed ${new Date(st.inv.fetched_at * 1000).toLocaleString()} · fetch again` : "Fetch again";
  // Every project / subscription the login can see: existing apps live in any of them.
  const current = st.inv.defaults[SCOPE_KEY[cloud]];
  $("cfg-scope-label").textContent = SCOPE_LABEL[cloud];
  const scopes = st.inv.scopes?.length ? st.inv.scopes : current ? [{ id: current, name: current }] : [];
  // Names only: the ID shows in the Project ID / Subscription ID field below. A repeated name keeps its ID.
  const seen = {};
  scopes.forEach((s) => { seen[s.name] = (seen[s.name] || 0) + 1; });
  fill($("cfg-scope"), scopes.map((s) => h("option", { value: s.id, selected: s.id === current, title: s.id,
    text: !s.name || s.name === s.id ? s.id : seen[s.name] > 1 ? `${s.name} (${s.id})` : s.name })));
  if (!st.dirty) {
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
  // One workload left to onboard: load it (fields, type and identity) without a click.
  const open = st.inv.workloads.map((w, i) => [w, i]).filter(([w]) => w.supported && !w.onboarded);
  if (open.length === 1 && !st.dirty) {
    sel.value = String(open[0][1]);
    pickWorkload();
  }
}

function pickWorkload() {
  const w = st.inv?.workloads[Number($("cfg-workload").value)];
  if (!w || !$("cfg-workload").value) return;
  st.cfg = freshConfig(w.kind);
  st.cfg.name = w.name;
  st.cfg.target[cloud].name = w.name;
  // The workload carries its own scope fields (project/region, subscription/resource group, account/region/profile/cluster,
  // context/cluster/namespace).
  for (const k of Object.keys(st.cfg.target[cloud])) if (k !== "name" && w[k] !== undefined && SCOPE_FIELDS[cloud].includes(k)) st.cfg.target[cloud][k] = w[k];
  $("cfg-discover").hidden = true;
  writeForm();
  markDirty(true);
  scheduleValidate();
  runDiscover();
}

// ----------------------------------------------------------------- help

const HELP = {
  "#step-app > h2": "The serverless workload that gets JFrog tokens. Pick it from the list or fill the fields.",
  "#cfg-scope": "The project or subscription to list workloads from.",
  "#cfg-workload": "An existing workload. Choosing one fills the fields below and finds its identity.",
  '[data-path="target.kind"]': "How the workload runs. It decides which Terraform module and wiring the pipeline uses.",
  '[data-path="name"]': "The name this configuration is saved under. Defaults to the workload's name; fixed once saved.",
  '[data-path="target.gcp.project"]': "The Google Cloud project that hosts the workload.",
  '[data-path="target.gcp.region"]': "The region the workload runs in, for example us-central1.",
  '[data-path="target.gcp.name"]': "The Cloud Run service, job or function name.",
  '[data-path="target.azure.subscription"]': "The Azure subscription that holds the workload.",
  '[data-path="target.azure.resource_group"]': "The resource group the workload is in.",
  '[data-path="target.azure.name"]': "The Container App or Function App name.",
  "#step-jfrog > h2": "The JFrog instance that trusts the workload's identity and issues its short-lived tokens.",
  '[data-path="jfrog.url"]': "Your JFrog Platform address, for example yourcompany.jfrog.io.",
  '[data-path="jfrog.reader_group"]': "The JFrog group the issued tokens belong to. It decides what the workload can read.",
  "#step-needs > h2": "How the workload gets JFrog tokens. Turn on one or both.",
  '[data-path="pull_path.enabled"]': "The platform pulls the container image from JFrog. A rotator keeps a short-lived pull token in Secret Manager or Key Vault and points the registry at it.",
  '[data-path="runtime.enabled"]': "The running code swaps its cloud identity for a JFrog token whenever it needs one, for example to download packages. Nothing is stored.",
  '[data-path="jfrog.docker_repo"]': "The JFrog Docker repository the images are pulled from.",
  '[data-path="pull_path.rotator_image"]': "The rotator job's image in Artifact Registry or ACR, pinned by digest.",
  '[data-path="target.azure.key_vault_id"]': "The Key Vault that holds the rotated pull token. The app reads it by reference.",
};

// ----------------------------------------------------------------- mount

// mode "new": empty form with the inventory picker; Save creates the workload (onSaved(name)).
// mode "edit": the saved configuration `name`; its name is fixed (renaming would make a second workload).
export async function mountConfig(mount, { schema, mode, name, config, onChange, onSaved }) {
  Object.assign(st, { schema, mode, name, onChange: onChange || (() => {}), onSaved, dirty: false, showAll: mode === "edit", inv: null });
  const frag = template("tpl-config");
  for (const el of frag.querySelectorAll("[data-cloud]")) if (el.dataset.cloud !== cloud) el.remove();
  mount.replaceChildren(frag);
  $("cfg-kind").replaceChildren(...cloudKinds().map((k) => h("option", { value: k, text: schema.kinds[k].label })));
  attachHelp(mount, HELP);

  $("cfg-form").addEventListener("input", onFieldInput);
  $("cfg-form").addEventListener("change", onFieldInput);
  $("cfg-form").addEventListener("submit", (e) => e.preventDefault());
  $("cfg-discover-btn").addEventListener("click", runDiscover);
  for (const b of document.querySelectorAll("#cfg-save")) b.addEventListener("click", save);
  $("cfg-preflight")?.addEventListener("click", runPreflight);

  if (mode === "edit") {
    $("cfg-pick").remove();
    const kind = cloudKinds().includes(config?.target?.kind) ? config.target.kind : cloudKinds()[0];
    st.cfg = mergeDeep(clone(schema.defaults[kind]), config);
    const nameEl = $("cfg-form").querySelector('[data-path="name"]');
    nameEl.readOnly = true;
    writeForm();
    markDirty(false);
    scheduleValidate();
    return;
  }
  st.cfg = freshConfig(cloudKinds()[0]);
  writeForm();
  markDirty(false);
  scheduleValidate();
  $("cfg-workload").addEventListener("change", pickWorkload);
  $("cfg-scope").addEventListener("change", () => loadInventory($("cfg-scope").value));
  $("cfg-inv-refresh").addEventListener("click", (e) => { e.preventDefault(); loadInventory($("cfg-scope").value, true); });
  loadInventory();
}

export const currentConfig = () => clone(st.cfg);
export const isDirty = () => st.dirty;
export const isBusy = () => st.busy;
