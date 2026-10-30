// KCP console UI. Vanilla ES module, no build step, no CDN.
// The session token comes from the URL fragment and is sent only as the X-Console-Token header.
// It is kept in localStorage for this origin so reloads and new tabs work: never in a cookie or the URL.

import { initOverview } from "./overview.js";
import { initConfigure } from "./configure.js";

const $ = (id) => document.getElementById(id);

const REDIRECTING = location.hostname === "localhost";
if (REDIRECTING) location.replace(location.href.replace("//localhost", "//127.0.0.1"));

function takeTokenFromHash() {
  const m = location.hash.match(/(?:^#|&)t=([A-Za-z0-9_-]+)/);
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  return m ? m[1] : null;
}

const TOKEN_KEY = "kcp-console-session-token";
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

// Shared state: the selected config and a tiny event bus ("config", "runs", "status").
export const state = {
  get token() { return TOKEN; },
  config: null, configs: [], started: false, session: null,
  listeners: {},
  on(evt, fn) { (this.listeners[evt] ||= []).push(fn); },
  emit(evt, data) { for (const fn of this.listeners[evt] || []) { try { fn(data); } catch (e) { console.error(e); } } },
};

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Console-Token": TOKEN || "" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit", cache: "no-store" });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (resp.status === 401) showAuth();
  if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || `HTTP ${resp.status}`);
  return data;
}

// replaceChildren() would turn null children into the text "null"; drop them.
export const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  fill(el, ...kids.map((k) => (typeof k === "string" || typeof k === "number" ? document.createTextNode(String(k)) : k)));
  return el;
}

// Inline `code` spans in runbook text, as text nodes only (never parsed as HTML).
export function rich(text) {
  return String(text ?? "").split(/(`[^`]+`)/).map((p) => (p.startsWith("`") && p.endsWith("`") && p.length > 2
    ? h("code", {}, p.slice(1, -1)) : document.createTextNode(p)));
}

function showAuth() {
  $("auth-banner").hidden = false;
  $("sidenav").hidden = true;
  document.querySelectorAll("[role=tabpanel]").forEach((p) => { p.hidden = true; });
}

// ----------------------------------------------------------------- pages

// One page, a side menu, and a URL per section: /clusters, /configure, /install … /simulate. The server
// serves index.html for all of them, so a reload or a shared link opens the same section.
const PAGES = ["overview", "configure"];
const pathOf = (p) => (p === "overview" ? "/clusters" : `/${p}`);

function fromPath(path) {
  const parts = path.replace(/\/+$/, "").split("/").slice(1);
  const name = parts[0] === "operate" ? parts[1] : parts[0];   // old /operate/<page> links
  return PAGES.includes(name) ? name : "overview";
}

export function selectTab(name, { history: how = "push" } = {}) {
  if (!PAGES.includes(name)) name = "overview";
  for (const p of PAGES) {
    $(`panel-${p}`).hidden = p !== name;
    $(`nav-${p}`).setAttribute("aria-current", p === name ? "page" : "false");
  }
  const path = pathOf(name);
  if (how !== "none" && location.pathname !== path) history[how === "replace" ? "replaceState" : "pushState"]({}, "", path);
  window.scrollTo(0, 0);
  state.emit("tab", name);
}

function wireTabs() {
  for (const p of PAGES) $(`nav-${p}`).addEventListener("click", () => selectTab(p));
}

$("auth-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const m = $("auth-input").value.match(/#t=([A-Za-z0-9_-]+)/) || $("auth-input").value.match(/^([A-Za-z0-9_-]{40,})$/);
  if (!m) return;
  TOKEN = rememberToken(m[1]);
  $("auth-input").value = "";
  start();
});

async function start() {
  if (REDIRECTING) return;
  if (!TOKEN) { showAuth(); return; }
  let session;
  try {
    session = await api("/api/session");
  } catch (err) {
    if (err.status !== 401) { $("auth-banner").hidden = false; $("auth-banner").querySelector("p").textContent = err.message; }
    return;
  }
  $("auth-banner").hidden = true;
  $("sidenav").hidden = false;
  const pill = $("mode-pill");
  pill.hidden = session.mode !== "replay";
  pill.textContent = "replay";
  state.started = true;
  state.session = session;
  await refreshConfigs();
  initOverview();
  for (const init of [initConfigure]) {
    try { init(); } catch (e) { console.error(e); }
  }
  state.emit("config", state.config);
  selectTab(fromPath(location.pathname), { history: "replace" });
  window.addEventListener("popstate", () => selectTab(fromPath(location.pathname), { history: "none" }));
}

export async function refreshConfigs(select) {
  const { configs } = await api("/api/configs");
  state.configs = configs;
  let want = select || state.config;
  try { want ||= localStorage.getItem("kcp-config"); } catch { /* ignore */ }
  if (!configs.some((c) => c.name === want)) want = configs.length ? configs[0].name : null;
  const sel = $("config-select");
  fill(sel, configs.length ? configs.map((c) => h("option", { value: c.name, selected: c.name === want }, `${c.name} · ${c.cluster}`))
    : [h("option", { value: "" }, "no saved config")]);
  $("config-pick").hidden = false;
  if (want !== state.config) {
    state.config = want;
    try { if (want) localStorage.setItem("kcp-config", want); } catch { /* ignore */ }
    if (select !== undefined || state.started) state.emit("config", want);
  }
}

$("config-select").addEventListener("change", (e) => {
  state.config = e.target.value || null;
  try { localStorage.setItem("kcp-config", state.config || ""); } catch { /* ignore */ }
  state.emit("config", state.config);
});



wireTabs();
window.addEventListener("hashchange", () => { const t = takeTokenFromHash(); if (t) { TOKEN = rememberToken(t); start(); } });
start();
