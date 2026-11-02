// The Kubernetes pages of the one JFrog OIDC Console: the session, API calls and DOM helpers are the
// console's own (/serverless/static/core.js), shared with /serverless and /setup.
import { $, h, fill, help, api as coreApi, hasToken, setToken, takeTokenFromHash, REDIRECTING, BASE } from "/serverless/static/core.js";

import { initClusters } from "./clusters.js";
import { initCluster } from "./cluster.js";
import { initRun } from "./run.js";

export { h, fill, help, BASE };
export const url = (path) => BASE + path;

// Shared state: the selected config and a tiny event bus ("config", "page").
export const state = {
  config: null, configs: [], started: false,
  listeners: {},
  on(evt, fn) { (this.listeners[evt] ||= []).push(fn); },
  emit(evt, data) { for (const fn of this.listeners[evt] || []) { try { fn(data); } catch (e) { console.error(e); } } },
};

// The console's api(); a 401 here asks for the sign-in link again.
export async function api(path, opts) {
  try { return await coreApi(path, opts); } catch (err) { if (err.status === 401) showAuth(); throw err; }
}

function showAuth() {
  $("auth-banner").hidden = false;
  document.querySelectorAll("[role=tabpanel]").forEach((p) => { p.hidden = true; });
}

// ----------------------------------------------------------------- pages

// One URL per page, each cloud kept apart (docs/ui-pattern.md):
//   /clusters                             every platform's clusters, one filtered list (clusters.js)
//   /clusters/<cloud>                     the same list filtered to one platform (aks, gke, openshift)
//   /clusters/<cloud>/<name>?tab=…        one cluster: Summary, Proof, Runs or Configuration
//   /clusters/<cloud>/<name>/runs/<id>    one run: its step tree and log
// /clusters, /clusters/<name>, /configure and .../<name>/edit are older links; they move to the paths above.
// The server serves index.html for all of them, so a reload or a shared link opens the same page.
export const CLOUDS = { aks: "Azure AKS", gke: "Google GKE", openshift: "OpenShift" };
export const TABS = { summary: "Summary", proof: "Proof", runs: "Runs", config: "Configuration" };
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
  if (!p[1]) return { page: "list", cloud: null, name: null };
  if (!p[2]) return { page: "list", cloud: p[1], name: null };
  if (p[3] === "edit") return { page: "cluster", cloud: p[1], name: p[2], tab: "config", moved: true };
  if (p[3] === "runs" && p[4]) return { page: "run", cloud: p[1], name: p[2], run: p[4] };
  return { page: "cluster", cloud: p[1], name: p[2], tab };
}

function canonical(r) {
  if (!r.name) return r.cloud ? `/clusters/${r.cloud}` : "/clusters";
  return `/clusters/${r.cloud}/${encodeURIComponent(r.name)}${r.tab && r.tab !== "summary" ? `?tab=${r.tab}` : ""}`;
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

// A product mark (aks, gke, openshift, k8s) or the arrow icon, cloned from the page's template.
export const markOf = (k) => $("marks").content.querySelector(`[data-mark="${k}"] svg`).cloneNode(true);

// Where you are: Kubernetes > <cluster> > Run <id>. Names come from the path.
function renderTrail(r) {
  const steps = [["Kubernetes", r.name ? "/clusters" : null]];
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
  // A section's own page needs no trail: the title already says where you are.
  $("trail").closest(".page-top").hidden = steps.length < 2;
}

let shown = "";
function show() {
  const r = route();
  if (r.moved) history.replaceState({}, "", url(canonical(r)) + (r.name ? "" : location.search));
  const now = route();
  for (const p of PAGES) $(`panel-${p}`).hidden = p !== now.page;
  window.consoleNav?.select("k8s");
  renderTrail(now);
  // A tab switch keeps the scroll position; a new page starts at the top.
  const key = `${now.page}|${now.name}|${now.run}`;
  if (key !== shown) window.scrollTo(0, 0);
  shown = key;
  state.emit("page", now);
}

// The sidebar's Kubernetes item goes through the router; any other link loads its page.
function wireTabs() {
  const a = document.querySelector('#sidenav [data-nav="k8s"]');
  a?.addEventListener("click", (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    go("/clusters");
  });
}

$("auth-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const m = $("auth-input").value.match(/#t=([A-Za-z0-9_-]+)/) || $("auth-input").value.match(/^([A-Za-z0-9_-]{40,})$/);
  if (!m) return;
  setToken(m[1]);
  $("auth-input").value = "";
  start();
});

async function start() {
  if (REDIRECTING) return;
  if (!hasToken()) { showAuth(); return; }
  let session;
  try {
    session = await api("/api/session");
  } catch (err) {
    if (err.status !== 401) { $("auth-banner").hidden = false; $("auth-banner").querySelector("p").textContent = err.message; }
    return;
  }
  $("auth-banner").hidden = true;
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
window.addEventListener("hashchange", () => { const t = takeTokenFromHash(); if (t) { setToken(t); start(); } });
start();
