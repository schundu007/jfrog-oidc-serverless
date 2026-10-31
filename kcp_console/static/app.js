// KCP console UI. Vanilla ES module, no build step, no CDN.
// The session token comes from the URL fragment and is sent only as the X-Console-Token header.
// It is kept in localStorage for this origin so reloads and new tabs work: never in a cookie or the URL.

import { initClusters } from "./clusters.js";
import { initCluster } from "./cluster.js";
import { initRun } from "./run.js";

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

// The path this console is served under: "" on its own, "/k8s" inside the unified console. Paths inside
// the app stay unprefixed; url() adds the base wherever the browser sees one (fetch, history, links).
export const BASE = (document.querySelector('meta[name="console-base"]') || {}).content || "";
export const url = (path) => BASE + path;

let TOKEN = REDIRECTING ? null : rememberToken(takeTokenFromHash() || pageToken()) || storedToken();

// Shared state: the selected config and a tiny event bus ("config", "page").
export const state = {
  config: null, configs: [], started: false,
  listeners: {},
  on(evt, fn) { (this.listeners[evt] ||= []).push(fn); },
  emit(evt, data) { for (const fn of this.listeners[evt] || []) { try { fn(data); } catch (e) { console.error(e); } } },
};

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Console-Token": TOKEN || "" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(url(path), { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
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

// A circled "?" that explains the thing next to it on hover or keyboard focus. The text is the
// button's description; clicking it never reaches an enclosing label's input.
let tips = 0;
export function help(text) {
  if (!text) return null;
  const id = `tip-${++tips}`;
  return h("span", { class: "help", tabindex: "0", role: "button", "aria-label": "What is this?", "aria-describedby": id,
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); } },
  "?", h("span", { class: "tip", role: "tooltip", id, "aria-hidden": "true" }, text));
}

function showAuth() {
  $("auth-banner").hidden = false;
  $("sidenav").hidden = true;
  $("k8s-sub").hidden = true;
  document.querySelectorAll("[role=tabpanel]").forEach((p) => { p.hidden = true; });
}

// ----------------------------------------------------------------- pages

// One URL per page, each cloud kept apart (docs/ui-pattern.md):
//   /clusters/<cloud>                     that platform's clusters (aks, gke, eks, openshift)
//   /clusters/<cloud>/<name>?tab=…        one cluster: Summary, Runs or Configuration
//   /clusters/<cloud>/<name>/runs/<id>    one run: its step tree and log
// /clusters, /clusters/<name>, /configure and .../<name>/edit are older links; they move to the paths above.
// The server serves index.html for all of them, so a reload or a shared link opens the same page.
export const CLOUDS = { aks: "Azure AKS", gke: "Google GKE", eks: "AWS EKS", openshift: "OpenShift" };
export const TABS = { summary: "Summary", runs: "Runs", config: "Configuration" };
const PAGES = ["list", "cluster", "run"];

export const cloudOf = (name) => (state.configs.find((c) => c.name === name) || {}).platform || "aks";
export const clusterPath = (name, tab) => `/clusters/${cloudOf(name)}/${encodeURIComponent(name)}${tab && tab !== "summary" ? `?tab=${tab}` : ""}`;
export const runPath = (name, id) => `/clusters/${cloudOf(name)}/${encodeURIComponent(name)}/runs/${id}`;

// {page, cloud, name, tab, run} from the address; moved: an older link to rewrite.
export function route(path = location.pathname) {
  if (path.startsWith(BASE)) path = path.slice(BASE.length) || "/";
  const p = path.replace(/\/+$/, "").split("/").slice(1).map(decodeURIComponent);
  const q = new URLSearchParams(location.search).get("tab");
  const tab = TABS[q] ? q : "summary";
  if (p[0] === "configure") return { page: "cluster", cloud: cloudOf(state.config), name: state.config, tab: "config", moved: true };
  if (p[0] !== "clusters") return { page: "list", cloud: null, name: null, moved: true };
  if (p[1] && !CLOUDS[p[1]]) return { page: "cluster", cloud: cloudOf(p[1]), name: p[1], tab, moved: true };
  if (!p[1]) return { page: "list", cloud: null, name: null, moved: true };
  if (!p[2]) return { page: "list", cloud: p[1], name: null };
  if (p[3] === "edit") return { page: "cluster", cloud: p[1], name: p[2], tab: "config", moved: true };
  if (p[3] === "runs" && p[4]) return { page: "run", cloud: p[1], name: p[2], run: p[4] };
  return { page: "cluster", cloud: p[1], name: p[2], tab };
}

// The first platform that has clusters, for /clusters.
const homeCloud = () => Object.keys(CLOUDS).find((k) => state.configs.some((c) => (c.platform || "aks") === k)) || "aks";

function canonical(r) {
  const cloud = r.cloud || homeCloud();
  if (!r.name) return `/clusters/${cloud}`;
  return `/clusters/${cloud}/${encodeURIComponent(r.name)}${r.tab && r.tab !== "summary" ? `?tab=${r.tab}` : ""}`;
}

// Go to a path inside the console without a reload.
export function go(path, { replace = false } = {}) {
  if (url(path) !== location.pathname + location.search) history[replace ? "replaceState" : "pushState"]({}, "", url(path));
  transition(show);
}

// Swap views with a short cross-fade where the browser can (never on reduced motion).
function transition(update) {
  if (!document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) return update();
  const t = document.startViewTransition(update);
  for (const p of [t.ready, t.finished, t.updateCallbackDone]) p.catch(() => {});
}

// One level up: run → its cluster's Runs tab → its platform's list → Home.
function parent(r) {
  if (r.page === "run") return { path: clusterPath(r.name, "runs"), label: r.name };
  if (r.name) return { path: `/clusters/${r.cloud}`, label: CLOUDS[r.cloud] };
  return { home: true, label: "Home" };
}

// A product mark (aks, gke, eks, openshift, k8s) or the arrow icon, cloned from the page's template.
export const markOf = (k) => $("marks").content.querySelector(`[data-mark="${k}"] svg`).cloneNode(true);

// Where you are: Kubernetes > <platform> > <cluster> > Run <id>. Names come from the path.
function renderTrail(r) {
  const steps = [["Kubernetes", null]];
  if (r.cloud) steps.push([CLOUDS[r.cloud], `/clusters/${r.cloud}`]);
  if (r.name) steps.push([r.name, r.page === "run" ? clusterPath(r.name, "runs") : clusterPath(r.name)]);
  if (r.page === "run") steps.push([`Run ${r.run}`, null]);
  const parts = [];
  steps.forEach(([label, path], i) => {
    if (i) parts.push(h("span", { class: "trail-sep", "aria-hidden": "true" }, "/"));
    if (i === steps.length - 1 || !path) return parts.push(h("span", { "aria-current": i === steps.length - 1 ? "page" : null }, label));
    parts.push(h("a", { href: url(path), onclick: (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      go(path);
    } }, label));
  });
  $("trail").replaceChildren(...parts);
}

// The open platform's clusters, as a band under it in the sidebar. It starts collapsed on every page
// load; clicking the open platform toggles it.
let bandOpen = false;
function renderBand(r) {
  const band = $("k8s-sub");
  const item = r.cloud && document.querySelector(`#sidenav [data-cloud="${r.cloud}"]`);
  const mine = state.configs.filter((c) => (c.platform || "aks") === r.cloud);
  document.querySelectorAll("#sidenav [data-cloud]").forEach((a) => a.removeAttribute("aria-expanded"));
  if (item && mine.length) item.setAttribute("aria-expanded", String(bandOpen));
  band.hidden = !item || !mine.length || !bandOpen;
  if (band.hidden) return;
  item.after(band);
  fill(band, mine.map((c) => h("a", { class: "nav-item", href: url(clusterPath(c.name)),
    "aria-current": c.name === r.name ? "page" : null, onclick: (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      go(clusterPath(c.name));
    } }, h("span", {}, c.name))));
}

// Back walks up one level, as the breadcrumb does.
function backButton(r) {
  const b = $("nav-back");
  const up = parent(r);
  b.hidden = !up;
  b.title = up ? `Back to ${up.label}` : "Back";
  b.onclick = () => {
    if (up?.home) location.assign("/");
    else if (up) go(up.path);
  };
}

let shown = "";
function show() {
  const r = route();
  if (r.moved) history.replaceState({}, "", url(canonical(r)) + (r.name ? "" : location.search));
  const now = route();
  for (const p of PAGES) $(`panel-${p}`).hidden = p !== now.page;
  document.querySelectorAll("#sidenav [data-cloud]").forEach((a) => {
    if (a.dataset.cloud === now.cloud) a.setAttribute("aria-current", now.name ? "true" : "page"); else a.removeAttribute("aria-current");
  });
  backButton(now);
  renderTrail(now);
  renderBand(now);
  // A tab switch keeps the scroll position; a new page starts at the top.
  const key = `${now.page}|${now.name}|${now.run}`;
  if (key !== shown) window.scrollTo(0, 0);
  shown = key;
  state.emit("page", now);
}

// Platform links go through the router; any other link (the other console's section) loads its page.
function wireTabs() {
  document.querySelectorAll("#sidenav [data-cloud]").forEach((a) => {
    const path = `/clusters/${a.dataset.cloud}`;
    a.setAttribute("href", url(path));
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      if (route().cloud === a.dataset.cloud && a.hasAttribute("aria-expanded")) {
        bandOpen = !bandOpen;
        renderBand(route());
        return;
      }
      bandOpen = false;
      go(path);
    });
  });
  $("sidenav").classList.add("k8s-nav");
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
  await refreshConfigs();
  initClusters();
  initCluster();
  initRun();
  state.emit("config", state.config);
  show();
  window.addEventListener("popstate", () => transition(show));
}

export async function refreshConfigs(select) {
  const { configs } = await api("/api/configs");
  state.configs = configs;
  let want = select || state.config;
  try { want ||= localStorage.getItem("kcp-config"); } catch { /* ignore */ }
  if (!configs.some((c) => c.name === want)) want = configs.length ? configs[0].name : null;
  if (want !== state.config) {
    state.config = want;
    try { if (want) localStorage.setItem("kcp-config", want); } catch { /* ignore */ }
    if (select !== undefined || state.started) state.emit("config", want);
  }
}

wireTabs();
window.addEventListener("hashchange", () => { const t = takeTokenFromHash(); if (t) { TOKEN = rememberToken(t); start(); } });
start();
