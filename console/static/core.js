// Shared plumbing for every page of the one console (/serverless, /setup): session token, API calls, event streams, DOM helpers,
// the one status icon set, time formats, the "⋯" menu, two-click confirmation and the flyout.
// Vanilla ES module, no build step, no CDN. Rendered with DOM APIs only (no innerHTML).

export const $ = (id) => document.getElementById(id);

// One origin only: the saved sign-in lives in 127.0.0.1's storage, so send localhost there.
// Nothing else runs on localhost, so the token in the fragment travels with the redirect.
export const REDIRECTING = location.hostname === "localhost";
if (REDIRECTING) location.replace(location.href.replace("//localhost", "//127.0.0.1"));

// Inside the portal this console lives under a prefix (/serverless): the server says so in a meta tag.
// Every API call, static file and page link carries it; localPath() is the path without it.
export const BASE = document.querySelector('meta[name="console-base"]')?.content || "";
export const localPath = () => (BASE && location.pathname.startsWith(BASE) ? location.pathname.slice(BASE.length) || "/" : location.pathname);

export const CLOUDS = { gcp: "Google Cloud", azure: "Azure" };
export const CLOUD = [localPath().split("/")[1], new URLSearchParams(location.search).get("cloud")]
  .find((c) => Object.hasOwn(CLOUDS, c)) || null;
export const ofCloud = (kind) => !!kind && String(kind).startsWith(`${CLOUD}_`);
// Each workload kind is its own item under the cloud's Serverless group in the menu (shell.js):
// ?type=<kind> lists that kind only; without it the list holds every kind of this cloud.
export const sectionOf = (kind) => window.consoleNav?.sectionOfKind(kind) || null;
const ofThisCloud = (kind) => !!kind && String(kind).startsWith(`${CLOUD}_`);
export const TYPE = ofThisCloud(new URLSearchParams(location.search).get("type")) ? new URLSearchParams(location.search).get("type") : null;
export const inSection = (kind) => ofCloud(kind) && (!TYPE || kind === TYPE);

// Page links. Names and run IDs are URL-safe (server-side patterns); encode anyway.
export const href = {
  // The one Serverless list, filtered to a type (which names its cloud) when given.
  list: (section = TYPE) => (section && /^[a-z]+_/.test(section) ? `${BASE}/?type=${encodeURIComponent(section)}` : `${BASE}/`),
  create: () => `${BASE}/${CLOUD}/new`,
  workload: (name, tab) => `${BASE}/${CLOUD}/w/${encodeURIComponent(name)}${tab ? `?tab=${tab}` : ""}`,
  run: (name, id) => `${BASE}/${CLOUD}/w/${encodeURIComponent(name)}/runs/${encodeURIComponent(id)}`,
};

// ----------------------------------------------------------------- session

// Read the token from the fragment and drop the fragment from the address bar and history straight away.
export function takeTokenFromHash() {
  const m = location.hash.match(/(?:^#|&)t=([A-Za-z0-9_-]+)/);
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  return m ? m[1] : null;
}

// Kept for this origin so a reload or a new tab does not lock the operator out. It is only valid while
// this console process runs. A token from the URL always wins (console restart). Never in a cookie or the URL.
export const TOKEN_KEY = "console-session-token";
export function rememberToken(t) {
  try { if (t) localStorage.setItem(TOKEN_KEY, t); } catch { /* storage blocked: memory only */ }
  return t;
}
function storedToken() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function forgetToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
}

// The server puts the token in the page when the operator opened it directly (no #t= needed).
function pageToken() {
  const m = document.querySelector('meta[name="console-token"]');
  const t = m ? m.content : null;
  if (m) m.remove();
  return t;
}

let TOKEN = REDIRECTING ? null : rememberToken(takeTokenFromHash() || pageToken()) || storedToken();
export const hasToken = () => !!TOKEN;
export const setToken = (t) => { TOKEN = rememberToken(t); };

export class ApiError extends Error {
  constructor(status, message, code, next) { super(message); this.status = status; this.code = code; this.next = next || null; }
}

// `abs`: a path of another portal section (/serverless/api/…, /setup/api/…), not under this page's base.
export async function api(path, { method = "GET", body, abs = false } = {}) {
  const headers = { "X-Console-Token": TOKEN };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch((abs ? "" : BASE) + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit", cache: "no-store" });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || `HTTP ${resp.status}`, data && data.code, data && data.next);
  return data;
}

// Server-sent events over fetch, so the token stays in a header (EventSource can't set headers).
// A POST stream (Preflight) passes { method: "POST", body }.
export async function streamEvents(path, onFrame, signal, { method = "GET", body } = {}) {
  const headers = { "X-Console-Token": TOKEN };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit", cache: "no-store", signal });
  if (!resp.ok || !resp.body) {
    let msg = `event stream failed: HTTP ${resp.status}`;
    try { msg = (await resp.json()).error || msg; } catch { /* not JSON */ }
    throw new ApiError(resp.status, msg);
  }
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

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// replaceChildren() would turn null children into the text "null"; drop them.
export const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));

// A page template from index.html, cloned.
export const template = (id) => $(id).content.cloneNode(true);

const NS = "http://www.w3.org/2000/svg";
function svgIcon(paths, cls) {
  const s = document.createElementNS(NS, "svg");
  s.setAttribute("viewBox", "0 0 16 16");
  s.setAttribute("aria-hidden", "true");
  if (cls) s.setAttribute("class", cls);
  for (const d of paths) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
}

// ----------------------------------------------------------------- status icons (one set everywhere)

// Filled circle + glyph: succeeded green check, failed dark-grey cross, running green spinner ring,
// waiting grey clock, cancelled grey slash, skipped grey dash, not run hollow ring. "warn" (a check
// that passed with a warning) is the grey circle with "!".
const STATUS_KEY = {
  succeeded: "succeeded", ok: "succeeded", pass: "succeeded",
  failed: "failed", fail: "failed", error: "failed",
  running: "running", busy: "running",
  waiting_approval: "waiting", needs_approval: "waiting", waiting: "waiting", queued: "waiting",
  cancelled: "cancelled", skipped: "skipped", skip: "skipped",
  pending: "notrun", notrun: "notrun", none: "notrun",
  warn: "warn", warning: "warn", degraded: "warn",
};
const GLYPH = {
  succeeded: ["M4.6 8.3l2.2 2.2 4.6-4.9"], failed: ["M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8"],
  waiting: ["M8 4.9V8l2.1 1.4"], cancelled: ["M5.2 10.8l5.6-5.6"], skipped: ["M5.2 8h5.6"],
  warn: ["M8 4.6v4.1", "M8 11.2v.1"],
};
const STATUS_LABEL = { waiting_approval: "waiting for approval", needs_approval: "waiting for approval", pending: "not run" };
export const statusKey = (s) => STATUS_KEY[s] || "notrun";
export const statusText = (s) => STATUS_LABEL[s] || String(s || "not run").replaceAll("_", " ");

export function statusIcon(status, label) {
  const k = statusKey(status);
  return h("span", { class: `si si-${k}`, role: "img", "aria-label": label || statusText(status), title: label || statusText(status) },
    GLYPH[k] ? svgIcon(GLYPH[k]) : null);
}

// A status pill: icon and word, toned by the status (green good, greys for everything else).
export function statusPill(status, text) {
  return h("span", { class: `pill pill-${statusKey(status)}` }, statusIcon(status), h("span", { text: text || statusText(status) }));
}

export const LIVE = new Set(["running", "waiting_approval"]);

// ----------------------------------------------------------------- time

const parse = (iso) => (iso ? Date.parse(iso) : NaN);

export { ago, stamp, clock, when, dur, duration } from "./time.js";

// A run's duration from its snapshot: created to the last step's end (now while it is live).
export function runDuration(snap) {
  if (!snap) return NaN;
  const ends = snap.steps.map((s) => parse(s.finished)).filter(Number.isFinite);
  const end = LIVE.has(snap.status) ? Date.now() : ends.length ? Math.max(...ends) : NaN;
  return end - parse(snap.created);
}

export function stepDuration(step) {
  const start = parse(step.started);
  const end = step.finished ? parse(step.finished) : step.status === "running" ? Date.now() : NaN;
  return end - start;
}

// ----------------------------------------------------------------- two-click confirmation

// Native confirm() dialogs block and are easy to dismiss: the first click arms the button for four seconds.
export function armed(btn, prompt) {
  if (btn.dataset.armed === "1") {
    btn.dataset.armed = "";
    btn.textContent = btn.dataset.label;
    return true;
  }
  btn.dataset.label ||= btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = prompt;
  setTimeout(() => { if (btn.dataset.armed === "1") { btn.dataset.armed = ""; btn.textContent = btn.dataset.label; } }, 4000);
  return false;
}

// ----------------------------------------------------------------- "⋯" menu

let openMenu = null;
function closeMenu() {
  if (!openMenu) return;
  openMenu.pop.hidden = true;
  openMenu.btn.setAttribute("aria-expanded", "false");
  openMenu = null;
}
document.addEventListener("click", (e) => { if (openMenu && !openMenu.wrap.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openMenu) { const b = openMenu.btn; closeMenu(); b.focus(); }
});

// items: [{ text, onSelect, confirm?, disabled?, danger? }]. An item with confirm needs a second click.
export function menu(label, items) {
  const pop = h("div", { class: "menu", role: "menu", hidden: true });
  const btn = h("button", { type: "button", class: "icon-btn", "aria-label": label, title: label, "aria-haspopup": "menu", "aria-expanded": "false" },
    svgIcon(["M3.5 8h.01M8 8h.01M12.5 8h.01"], "dots"));
  const wrap = h("span", { class: "menu-wrap" }, btn, pop);
  for (const it of items.filter(Boolean)) {
    const item = h("button", { type: "button", role: "menuitem", class: `menu-item${it.danger ? " danger" : ""}`, text: it.text,
      disabled: it.disabled || null, title: it.title || null });
    item.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (it.confirm && !armed(item, it.confirm)) return;
      closeMenu();
      await it.onSelect();
    });
    pop.append(item);
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const was = openMenu?.btn === btn;
    closeMenu();
    if (was) return;
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    openMenu = { btn, pop, wrap };
    pop.querySelector("button:not(:disabled)")?.focus();
  });
  pop.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = [...pop.querySelectorAll("button:not(:disabled)")];
    const i = items.indexOf(document.activeElement);
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
  });
  return wrap;
}

// ----------------------------------------------------------------- flyout (right-hand side panel)

// One panel, filled by whoever opens it (Preflight). Close button, Escape, or opening another closes it.
export function flyout(title) {
  const panel = $("flyout");
  panel.hidden = false;
  $("flyout-title").textContent = title;
  fill($("flyout-status"));
  fill($("flyout-body"));
  return { status: $("flyout-status"), body: $("flyout-body") };
}
export function closeFlyout() { $("flyout").hidden = true; }

// ----------------------------------------------------------------- help popups

// A circled "?" after a heading or field label; its explanation pops up on hover or keyboard focus.
// Clicking it never toggles the checkbox it sits next to.
let tips = 0;
export function help(text) {
  if (!text) return null;
  const id = `tip-${++tips}`;
  const tip = h("span", { class: "tip", role: "tooltip", id, "aria-hidden": "true" }, text);
  const el = h("span", { class: "help", tabindex: "0", role: "button", "aria-label": "What is this?", "aria-describedby": id,
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); } }, "?", tip);
  // Fixed to the viewport, placed on show: no card or table that clips its overflow can hide it.
  const place = () => {
    const r = el.getBoundingClientRect();
    const above = r.top > tip.offsetHeight + 16;
    tip.classList.toggle("tip-below", !above);
    const half = Math.min(tip.offsetWidth / 2, r.left + r.width / 2 - 8);
    tip.style.left = `${Math.max(8 + half, Math.min(r.left + r.width / 2, window.innerWidth - 8 - tip.offsetWidth / 2))}px`;
    tip.style.top = above ? `${r.top - 10}px` : `${r.bottom + 10}px`;
  };
  el.addEventListener("mouseenter", place);
  el.addEventListener("focus", place);
  return el;
}

// Static pages mark an explanation with data-help="…": it becomes the element's circled "?".
export function helpFromData(root = document) {
  for (const el of root.querySelectorAll("[data-help]")) {
    if (el.dataset.help && !el.querySelector(":scope > .help")) el.append(help(el.dataset.help));
  }
}
if (typeof document !== "undefined") helpFromData();

export function attachHelp(root, table) {
  for (const [sel, text] of Object.entries(table)) {
    const el = root.querySelector(sel);
    if (!el) continue;
    const target = el.matches("h2") ? el
      : el.closest("label.choice")?.querySelector("b") || el.closest("label")?.querySelector(":scope > span");
    if (target && !target.querySelector(".help")) target.append(help(text));
  }
}

// ----------------------------------------------------------------- page status line

export function status(el, text, kind = "") {
  if (!el) return;
  el.textContent = text || "";
  el.className = `page-status small${kind ? ` status-${kind}` : ""}`;
}

// What to do after a refusal or a failed step: the server's {text, action?, url?, link?}.
// `onAction(steps)` starts the offered run (Deliver now); a url opens the ADO run in a new tab.
export function nextBox(next, onAction) {
  if (!next || !next.text) return null;
  const box = h("div", { class: "next-box" }, h("b", { text: "Next" }), h("span", { text: next.text }));
  if (next.url) box.append(h("a", { href: next.url, target: "_blank", rel: "noopener noreferrer", text: "Open run" }));
  if (next.link) box.append(h("a", { href: next.link.href, text: next.link.label }));
  if (next.action && onAction) {
    const b = h("button", { type: "button", class: "small-btn", text: next.action.label });
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await onAction(next.action.steps); } catch (e) { b.disabled = false; b.title = e.message; }
    });
    box.append(b);
  }
  return box;
}

// Plain editors for lists and name/value pairs, so no form ever asks for JSON. Each returns an
// element with .value() giving the list (blanks dropped) or the object (blank names dropped).
// With options (a promise of [{id, name}] from live data), each row offers them and is tinted:
// green when the value exists there, red when it does not (a pipeline would fail on it).
export function markKnown(input, knownOf) {
  const check = () => {
    const v = input.value.trim().toLowerCase(), known = knownOf();
    input.classList.toggle("is-known", Boolean(v && known?.has(v)));
    input.classList.toggle("is-unknown", Boolean(v && known && !known.has(v)));
  };
  input.addEventListener("input", check);
  check();
  return check;
}

let lists = 0;
export function listEditor(values = [], { label = "value", placeholder = "", options = null } = {}) {
  const rows = h("div", { class: "kv-rows" });
  const listId = `opts-${++lists}`;
  const datalist = h("datalist", { id: listId });
  let known = null;
  const checks = [];
  options?.then((opts) => {
    known = new Set(opts.map((o) => String(o.id).toLowerCase()));
    datalist.replaceChildren(...opts.map((o) => h("option", { value: o.id, text: o.name })));
    checks.forEach((c) => c());
  }).catch(() => {});
  const add = (v = "") => {
    const input = h("input", { type: "text", value: v, placeholder, spellcheck: "false", "aria-label": label, list: listId });
    checks.push(markKnown(input, () => known));
    const row = h("div", { class: "kv-row kv-one" }, input,
      h("button", { type: "button", class: "icon", "aria-label": `Remove ${label}`, text: "✕", onclick: () => row.remove() }));
    rows.append(row);
    return input;
  };
  (values.length ? values : [""]).forEach((v) => add(String(v)));
  const el = h("div", { class: "kv" }, rows, datalist,
    h("button", { type: "button", class: "secondary small-btn", text: "+ Add", onclick: () => add().focus() }));
  el.value = () => [...rows.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean);
  return el;
}

export function mapEditor(values = {}, { keyLabel = "name", valueLabel = "value" } = {}) {
  const rows = h("div", { class: "kv-rows" });
  const add = (k = "", v = "") => {
    const key = h("input", { type: "text", value: k, placeholder: keyLabel, spellcheck: "false", "aria-label": keyLabel, class: "kv-key" });
    const val = h("input", { type: "text", value: v, placeholder: valueLabel, spellcheck: "false", "aria-label": valueLabel, class: "kv-val" });
    const row = h("div", { class: "kv-row" }, key, val,
      h("button", { type: "button", class: "icon", "aria-label": `Remove ${keyLabel}`, text: "✕", onclick: () => row.remove() }));
    rows.append(row);
    return key;
  };
  const entries = Object.entries(values || {});
  (entries.length ? entries : [["", ""]]).forEach(([k, v]) => add(k, Array.isArray(v) ? v.join(", ") : String(v)));
  const el = h("div", { class: "kv" }, rows,
    h("button", { type: "button", class: "secondary small-btn", text: "+ Add", onclick: () => add().focus() }));
  el.value = () => Object.fromEntries([...rows.children]
    .map((r) => [r.querySelector(".kv-key").value.trim(), r.querySelector(".kv-val").value.trim()]).filter(([k]) => k));
  return el;
}

// Every Refresh button: disabled with "Refreshing…" while fn runs, then "Refreshed" for a moment (or
// the error in its tooltip), so a click is always seen to do something.
export function refreshing(btn, fn) {
  const label = [...btn.childNodes].find((n) => n.nodeType === 3 && n.textContent.trim());
  const say = (t) => { if (label) label.textContent = t; };
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("is-busy");
    say("Refreshing…");
    try {
      await fn();
      say("Refreshed");
      btn.title = `Refreshed ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      say("Refresh failed");
      btn.title = err?.message || String(err);
    } finally {
      btn.disabled = false;
      btn.classList.remove("is-busy");
      setTimeout(() => say("Refresh"), 2000);
    }
  });
}
