// Dashboard: both sections at a glance, read-only. Serverless data from /serverless/api, Kubernetes
// from /k8s/api, with the one session token (the page carries it when opened directly).
const $ = (id) => document.getElementById(id);
const MARK = {"gcp": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M7.2 18.6h9.6a4.4 4.4 0 0 0 .5-8.8A5.9 5.9 0 0 0 6 9.2a4.7 4.7 0 0 0 1.2 9.4z\" pathLength=\"100\" fill=\"none\" stroke=\"#ea4335\" stroke-width=\"2.2\" stroke-dasharray=\"25 75\" stroke-dashoffset=\"0\"/><path d=\"M7.2 18.6h9.6a4.4 4.4 0 0 0 .5-8.8A5.9 5.9 0 0 0 6 9.2a4.7 4.7 0 0 0 1.2 9.4z\" pathLength=\"100\" fill=\"none\" stroke=\"#fbbc04\" stroke-width=\"2.2\" stroke-dasharray=\"25 75\" stroke-dashoffset=\"-25\"/><path d=\"M7.2 18.6h9.6a4.4 4.4 0 0 0 .5-8.8A5.9 5.9 0 0 0 6 9.2a4.7 4.7 0 0 0 1.2 9.4z\" pathLength=\"100\" fill=\"none\" stroke=\"#34a853\" stroke-width=\"2.2\" stroke-dasharray=\"25 75\" stroke-dashoffset=\"-50\"/><path d=\"M7.2 18.6h9.6a4.4 4.4 0 0 0 .5-8.8A5.9 5.9 0 0 0 6 9.2a4.7 4.7 0 0 0 1.2 9.4z\" pathLength=\"100\" fill=\"none\" stroke=\"#4285f4\" stroke-width=\"2.2\" stroke-dasharray=\"25 75\" stroke-dashoffset=\"-75\"/></svg>", "azure": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M9.7 3.2h4.5L7.9 20.8H3.2z\" fill=\"#0062ad\"/><path d=\"M14.8 7.4l6 13.4H9.6l4-1.6-3.6-.2z\" fill=\"#2a95e8\"/></svg>", "aws": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><text x=\"12\" y=\"12\" text-anchor=\"middle\" font-size=\"8.6\" font-weight=\"700\" font-family=\"Arial, Helvetica, sans-serif\" fill=\"currentColor\">aws</text><path d=\"M4 15.2c4.7 3 11.3 3.2 16 .2\" fill=\"none\" stroke=\"#ff9900\" stroke-width=\"2\" stroke-linecap=\"round\"/><path d=\"M17.3 14l3 .9-.9 3\" fill=\"none\" stroke=\"#ff9900\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg>", "openshift": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><circle cx=\"12\" cy=\"12\" r=\"8\" fill=\"none\" stroke=\"#e00\" stroke-width=\"3.4\"/><path class=\"cut\" d=\"M2.5 10.2l19-4.6M2.5 17.6l19-4.6\" fill=\"none\" stroke-width=\"1.8\"/></svg>"};
const PLATFORMS = [["aks", "azure", "Azure AKS"], ["gke", "gcp", "Google GKE"], ["eks", "aws", "AWS EKS"], ["openshift", "openshift", "OpenShift"]];
const CLOUDS = [["gcp", "Google Cloud"], ["azure", "Azure"]];
const RUN = { succeeded: "ok", failed: "bad", cancelled: "bad", running: "info", waiting_approval: "warn" };
const STATE = { working: ["ok", "OIDC working"], attention: ["bad", "Needs attention"], not_proven: ["bad", "Not proven"],
  approval: ["warn", "Awaiting approval"], unverified: ["warn", "Not verified"], running: ["info", "Running"],
  not_installed: ["", "Not set up"], unread: ["", "Not read"] };

function token() {
  const m = document.querySelector('meta[name="console-token"]');
  if (m) { try { localStorage.setItem("console-session-token", m.content); } catch { /* memory only */ } return m.content; }
  try { return localStorage.getItem("console-session-token"); } catch { return null; }
}
const TOKEN = token();

async function get(path) {
  const r = await fetch(path, { headers: { "X-Console-Token": TOKEN || "" }, credentials: "omit", cache: "no-store" });
  if (!r.ok) throw new Error(r.status === 401 ? "signed-out" : `HTTP ${r.status}`);
  return r.json();
}

function h(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) {
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v; else n.setAttribute(k, v);
  }
  n.append(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  return n;
}
const markEl = (k) => { const s = h("span", { class: "dash-mark" }); s.innerHTML = MARK[k]; return s; };  // constant SVG only
const when = (t) => (t || "").replace("T", " ").replace(/:\d\dZ$/, " UTC").replace("Z", " UTC");
const kpi = (n, label, cls = "") => h("div", { class: `kpi ${cls}` }, h("b", { text: String(n) }), h("span", { text: label }));
const row = (href, ...kids) => h("a", { class: "dash-row", href }, ...kids);
// A tile: big product mark, name, then its numbers, stacked and centered.
function tile(href, markKey, name, line, pill) {
  const m = h("span", { class: "tile-mark" }); m.innerHTML = MARK[markKey];  // constant SVG only
  return h("a", { class: "tile", href }, m, h("b", { text: name }), h("small", { text: line }), pill);
}
const empty = (t) => h("p", { class: "muted dash-empty", text: t });

async function serverless() {
  const [{ configs }, { runs }, schema] = await Promise.all([get("/serverless/api/configs"), get("/serverless/api/runs"),
    get("/serverless/api/schema")]);
  const label = (k) => schema.kinds?.[k]?.label || k;
  const cloudOf = (k) => String(k).split("_")[0];
  const count = (s) => runs.filter((r) => r.status === s).length;
  $("sl-kpis").replaceChildren(kpi(configs.length, "Configurations"), kpi(configs.filter((c) => c.identity).length, "Identities discovered"),
    kpi(count("succeeded"), "Runs succeeded", "ok"), kpi(count("failed") + count("cancelled"), "Runs failed", "bad"),
    kpi(count("running") + count("waiting_approval"), "Runs in progress", "info"));
  $("sl-clouds").replaceChildren(...CLOUDS.map(([c, name]) => {
    const n = configs.filter((x) => cloudOf(x.kind) === c).length;
    const last = runs.find((r) => cloudOf(r.kind) === c);
    return tile(`/serverless/${c}`, c, name, `${n} configuration${n === 1 ? "" : "s"}`,
      last ? h("span", { class: `pill ${RUN[last.status] || ""}`, text: `last run ${last.status.replaceAll("_", " ")}` }) : null);
  }));
  $("sl-runs").replaceChildren(...(runs.length ? runs.slice(0, 6).map((r) => row(`/serverless/${cloudOf(r.kind)}/runs`,
    markEl(cloudOf(r.kind)), h("span", { class: "dash-main" }, h("b", { text: r.name }), h("small", { text: `${label(r.kind)} · ${when(r.created)}` })),
    h("span", { class: `pill ${RUN[r.status] || ""}`, text: r.status.replaceAll("_", " ") }))) : [empty("No runs")]));
}

async function kubernetes() {
  const fleet = await get("/k8s/api/fleet");
  const cl = fleet.clusters || [];
  const by = (s) => cl.filter((c) => c.state === s).length;
  $("k8s-kpis").replaceChildren(kpi(cl.length, "Clusters"), kpi(by("working"), "OIDC working", "ok"),
    kpi(by("attention") + by("not_proven"), "Need attention", "bad"), kpi(by("approval"), "Awaiting approval", "warn"),
    kpi(by("not_installed") + by("unread"), "Not set up", ""));
  $("k8s-plats").replaceChildren(...PLATFORMS.map(([p, m, name]) => {
    const mine = cl.filter((c) => c.cloud === p);
    const ok = mine.filter((c) => c.state === "working").length;
    return tile(`/k8s/clusters/${p}`, m, name, `${mine.length} cluster${mine.length === 1 ? "" : "s"}`,
      mine.length ? h("span", { class: `pill ${ok === mine.length ? "ok" : "warn"}`, text: `${ok}/${mine.length} working` }) : null);
  }));
  const pm = Object.fromEntries(PLATFORMS.map(([p, m]) => [p, m]));
  $("k8s-clusters").replaceChildren(...(cl.length ? cl.map((c) => {
    const [cls, text] = STATE[c.state] || ["", c.state];
    return row(`/k8s/clusters/${c.cloud}/${encodeURIComponent(c.name)}`, markEl(pm[c.cloud] || "gcp"),
      h("span", { class: "dash-main" }, h("b", { text: c.name }), h("small", { text: c.problem || (c.verified_at ? `verified ${when(c.verified_at)}` : c.cluster || "") })),
      h("span", { class: `pill ${cls}`, text }));
  }) : [empty("No clusters")]));
}

async function load() {
  const results = await Promise.allSettled([serverless(), kubernetes()]);
  const out = results.some((r) => r.status === "rejected" && r.reason?.message === "signed-out");
  $("session-state").hidden = !out;
  $("session-state").textContent = "Not signed in";
  results.forEach((r, i) => { if (r.status === "rejected" && r.reason?.message !== "signed-out") {
    $(i ? "k8s-kpis" : "sl-kpis").replaceChildren(h("p", { class: "error small", text: r.reason.message })); } });
}
$("dash-refresh").addEventListener("click", load);
load();
