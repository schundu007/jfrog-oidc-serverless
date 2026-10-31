// JFrog OIDC console UI (Serverless). Vanilla ES module, no build step, no CDN.
// The session token comes from the URL fragment (or the page) and is sent only as the X-Console-Token
// header; see core.js. Pages, modelled on Azure DevOps Pipelines (docs/ui-pattern.md):
//   /<cloud>                        Workloads (list.js)
//   /<cloud>/new                    New workload: the configuration form; Save creates it and opens it
//   /<cloud>/w/<name>?tab=…         Workload: Runs · Configuration · Simulator (workload.js)
//   /<cloud>/w/<name>/runs/<id>     Run: step tree, step detail and log (runs.js)
// Old paths (/<cloud>/configure, /runs, /simulate, /configs) land on Workloads.

import { $, api, h, BASE, CLOUD, CLOUDS, REDIRECTING, localPath, href, hasToken, setToken, takeTokenFromHash, forgetToken,
  closeFlyout, template } from "./core.js";
import { mountList } from "./list.js";
import { mountWorkload } from "./workload.js";
import { mountRun } from "./runs.js";
import { mountConfig } from "./configure.js";
import { NOT_YET, mountNotYet } from "./notyet.js";

const RUN_ID = /^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/;
const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

// Which page this path is. Anything else under the cloud (old tabs) is the list.
function route() {
  const parts = localPath().split("/").filter(Boolean).map(decodeURIComponent);
  const [, a, name, r, id] = parts;
  if (a === "new" && parts.length === 2) return { page: "new" };
  if (a === "w" && NAME.test(name || "") && parts.length === 3) return { page: "workload", name };
  if (a === "w" && NAME.test(name || "") && r === "runs" && RUN_ID.test(id || "") && parts.length === 5) return { page: "run", name, id };
  return { page: "list", legacy: parts.length > 1 };
}

// Where you are; every level above the current page is a link.
function renderTrail(r) {
  const steps = [["Serverless", null], [CLOUDS[CLOUD], null]];
  if (r.page !== "not-yet") steps.push(["Workloads", r.page === "list" ? null : href.list()]);
  if (r.page === "new") steps.push(["New workload", null]);
  if (r.name) steps.push([r.name, r.page === "run" ? href.workload(r.name, "runs") : null]);
  if (r.page === "run") steps.push([`Run ${r.id}`, null]);
  const parts = [];
  steps.forEach(([label, link], i) => {
    if (i) parts.push(h("span", { class: "trail-sep", "aria-hidden": "true", text: "/" }));
    parts.push(link ? h("a", { href: link, text: label })
      : h("span", { "aria-current": i === steps.length - 1 ? "page" : null, text: label }));
  });
  $("trail").replaceChildren(...parts);
}

// Keep only this cloud's links; inside the portal, links to this console's pages carry the prefix.
function applyShell() {
  if (BASE) {
    // The switch to the Kubernetes section (a different console) is a full page load.
    for (const a of document.querySelectorAll('a[href^="/"]:not([data-section])')) a.setAttribute("href", BASE + a.getAttribute("href"));
    for (const el of document.querySelectorAll("[data-portal]")) el.hidden = false;
  }
  document.title = `JFrog OIDC Console · ${CLOUDS[CLOUD]}`;
  document.querySelector(`[data-cloud-link="${CLOUD}"]`)?.setAttribute("aria-current", "page");
  $("cloud-switch").hidden = false;
  $("flyout-close").addEventListener("click", closeFlyout);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("flyout").hidden && !document.querySelector(".menu:not([hidden])")) closeFlyout(); });
}

// ----------------------------------------------------------------- session

function sessionState(text, ok) {
  const pill = $("session-state");
  pill.textContent = text;
  pill.hidden = ok;
}

function authError(msg) {
  $("auth-banner").hidden = false;
  let p = $("auth-error");
  if (!p) $("auth-form").after(p = h("p", { id: "auth-error", class: "error small", role: "alert" }));
  p.textContent = msg || "";
  p.hidden = !msg;
}

// Load the schema with the current token. Returns it, or null (and says why).
async function connect() {
  if (!hasToken()) {
    $("auth-banner").hidden = false;
    sessionState("no token", false);
    return null;
  }
  try {
    const schema = await api("/api/schema");
    $("auth-banner").hidden = true;
    sessionState("local session", true);
    return schema;
  } catch (e) {
    sessionState(e.status === 401 ? "token rejected" : "offline", false);
    if (e.status === 401) { forgetToken(); authError("Session token rejected. Paste the URL the console printed."); }
    else authError(e.message);
    return null;
  }
}

// ----------------------------------------------------------------- pages

async function mountNew(page, { schema }) {
  page.replaceChildren(template("tpl-new"));
  document.title = "New workload · Workloads";
  $("new-cancel").setAttribute("href", href.list());
  await mountConfig($("cfg-mount"), { schema, mode: "new", onSaved: (name) => location.assign(href.workload(name)) });
}

let mounted = false;
async function start() {
  const schema = await connect();
  if (!schema || mounted) return;
  mounted = true;
  // AWS and OpenShift: not set up live yet, every path shows what they will need.
  const r = NOT_YET[CLOUD] ? { page: "not-yet" } : route();
  if (r.legacy) history.replaceState(null, "", href.list() + location.search);
  renderTrail(r);
  const page = $("page");
  try {
    if (r.page === "not-yet") mountNotYet(page, CLOUD);
    else if (r.page === "new") await mountNew(page, { schema });
    else if (r.page === "workload") await mountWorkload(page, { name: r.name, schema });
    else if (r.page === "run") await mountRun(page, { name: r.name, id: r.id });
    else await mountList(page, { schema });
  } catch (e) {
    page.append(h("p", { class: "error-box", role: "alert", text: e.message }));
  }
}

function boot() {
  // No picker page: the console opens on Google Cloud; the sidebar switches clouds.
  if (!CLOUD) {
    location.replace(`${BASE}/gcp${location.search}`);
    return;
  }
  applyShell();
  window.addEventListener("hashchange", () => {
    const t = takeTokenFromHash();
    if (t) { setToken(t); start(); }
  });
  $("auth-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const raw = $("auth-input").value.trim();
    const m = raw.match(/(?:#|&|^)t=([A-Za-z0-9_-]+)/) || raw.match(/^([A-Za-z0-9_-]{20,})$/);
    $("auth-input").value = "";
    if (!m) return authError("That isn't the console URL or token. Copy the whole line that ends in #t=…");
    setToken(m[1]);
    start();
  });
  start();
}

if (!REDIRECTING) boot();
