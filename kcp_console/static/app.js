// KCP console UI. Vanilla ES module, no build step, no CDN.
// The session token comes from the URL fragment and is sent only as the X-Console-Token header.
// It is kept in localStorage for this origin so reloads and new tabs work: never in a cookie or the URL.

import { initOverview } from "./overview.js";
import { initDocs } from "./docs.js";
import { initSimulate } from "./simulate.js";
import { initConfigure } from "./configure.js";
import { initInstall } from "./install.js";
import { initNodes } from "./nodes.js";
import { initArtifactory } from "./artifactory.js";
import { initVerify } from "./verify.js";
import { initTroubleshoot } from "./troubleshoot.js";
import { initDemo } from "./demo.js";
import { initSecurity } from "./security.js";
import { initRollback } from "./rollback.js";
import { initRuns } from "./runs.js";

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
  config: null, configs: [], knowledge: null, session: null,
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
  $("tabs").hidden = true;
  document.querySelectorAll("[role=tabpanel]").forEach((p) => { p.hidden = true; });
}

// ----------------------------------------------------------------- tabs

const TABS = ["overview", "configure", "operate", "runs", "simulate", "docs"];
// Pages inside Operate, picked from its side menu.
const SUBS = ["install", "verify", "nodes", "artifactory", "troubleshoot", "security", "demo", "rollback"];
export function selectTab(name) {
  let sub = null;
  if (SUBS.includes(name)) { sub = name; name = "operate"; }
  if (!TABS.includes(name)) name = "overview";
  for (const t of TABS) {
    const on = t === name;
    $(`tab-${t}`).setAttribute("aria-selected", String(on));
    $(`tab-${t}`).tabIndex = on ? 0 : -1;
    $(`panel-${t}`).hidden = !on;
  }
  if (name === "operate") {
    try { sub ||= sessionStorage.getItem("kcp-sub") || "install"; } catch { sub ||= "install"; }
    if (!SUBS.includes(sub)) sub = "install";
    for (const s of SUBS) {
      $(`panel-${s}`).hidden = s !== sub;
      $(`sub-${s}`).setAttribute("aria-current", s === sub ? "page" : "false");
    }
    try { sessionStorage.setItem("kcp-sub", sub); } catch { /* ignore */ }
  }
  try { sessionStorage.setItem("kcp-tab", name); } catch { /* ignore */ }
}

function wireTabs() {
  for (const t of TABS) $(`tab-${t}`).addEventListener("click", () => selectTab(t));
  for (const s of SUBS) $(`sub-${s}`).addEventListener("click", () => selectTab(s));
  $("tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const i = TABS.findIndex((t) => $(`tab-${t}`).getAttribute("aria-selected") === "true");
    const next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
    selectTab(next);
    $(`tab-${next}`).focus();
  });
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
  let session, knowledge;
  try {
    [session, knowledge] = await Promise.all([api("/api/session"), api("/api/knowledge")]);
  } catch (err) {
    if (err.status !== 401) { $("auth-banner").hidden = false; $("auth-banner").querySelector("p").textContent = err.message; }
    return;
  }
  $("auth-banner").hidden = true;
  $("tabs").hidden = false;
  const pill = $("mode-pill");
  pill.hidden = false;
  pill.textContent = session.mode === "replay" ? "replay: recorded fixtures" : "live: read-only until approved";
  state.knowledge = knowledge;
  state.session = session;
  await refreshConfigs();
  initOverview(knowledge);
  initSimulate({ api });
  for (const init of [initConfigure, initInstall, initNodes, initArtifactory, initVerify, initTroubleshoot, initDemo,
    initSecurity, initRollback, initRuns, initDocs]) {
    try { init(); } catch (e) { console.error(e); }
  }
  state.emit("config", state.config);
  let tab = "overview";
  try { tab = sessionStorage.getItem("kcp-tab") || tab; } catch { /* ignore */ }
  selectTab(TABS.includes(tab) || SUBS.includes(tab) ? tab : "overview");
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
  $("report-link").hidden = !want;
  if (want !== state.config) {
    state.config = want;
    try { if (want) localStorage.setItem("kcp-config", want); } catch { /* ignore */ }
    if (select !== undefined || state.knowledge) state.emit("config", want);
  }
}

$("config-select").addEventListener("change", (e) => {
  state.config = e.target.value || null;
  try { localStorage.setItem("kcp-config", state.config || ""); } catch { /* ignore */ }
  $("report-link").hidden = !state.config;
  state.emit("config", state.config);
});

// The report is fetched with the token header, then handed to the browser as a download.
$("report-link").addEventListener("click", async () => {
  if (!state.config) return;
  const resp = await fetch(`/api/report/${encodeURIComponent(state.config)}?format=md`, {
    headers: { "X-Console-Token": TOKEN }, credentials: "omit", cache: "no-store" });
  if (!resp.ok) return;
  const url = URL.createObjectURL(await resp.blob());
  const a = h("a", { href: url, download: `kcp-report-${state.config}.md` });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

wireTabs();
window.addEventListener("hashchange", () => { const t = takeTokenFromHash(); if (t) { TOKEN = rememberToken(t); start(); } });
start();
