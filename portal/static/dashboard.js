// Home, in the Azure portal's resource style: title and command bar, Essentials (label : value, two
// columns), then tabs: Serverless workloads, Kubernetes clusters (runs have their own page). Each tab is one filter
// bar ("Filter for any field…" and pills) over one table. Read-only. Data: data.js.
import { CLOUD_NAMES, LIVE_STATES, h, markEl, when, runPill, loadAll } from "./data.js";

const $ = (id) => document.getElementById(id);
const TABS = ["serverless", "kubernetes"];
const u0 = new URLSearchParams(location.search);
const S = { tab: TABS.includes(u0.get("tab")) ? u0.get("tab") : "serverless", q: u0.get("q") || "", cloud: u0.get("cloud") || "",
  type: u0.get("type") || "", status: u0.get("status") || "" };
let data = { items: [], runs: [] };

function saveQuery() {
  const u = new URLSearchParams();
  for (const k of ["tab", "q", "cloud", "type", "status"]) if (S[k] && !(k === "tab" && S.tab === "serverless")) u.set(k, S[k]);
  history.replaceState(null, "", `/${u.toString() ? `?${u}` : ""}`);
}

// Status as the portal shows it: a coloured dot and words.
const DOT = { ok: "ok", warn: "warn", bad: "fail", info: "warn" };
const stateCell = ([cls, text]) => h("td", {}, h("span", { class: `blade-state ${DOT[cls] || ""}`, text }));
const nameCell = (mark, text, href) => h("td", {}, h("span", { class: "blade-name" }, mark ? markEl(mark) : null,
  href ? h("a", { href, text }) : h("span", { text })));
function rowOf(href, cells) {
  const tr = h("tr", { class: href ? "row-link" : null }, ...cells);
  if (href) tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.assign(href); });
  return tr;
}

// Each tab: its rows, its columns, how a row reads, and what its Status pill offers.
const VIEW = {
  serverless: {
    rows: () => data.items.filter((it) => it.kind === "workload"), empty: "No workloads onboarded yet",
    cols: ["Name", "Status", "Type", "Last run"],
    row: (it) => rowOf(it.href, [nameCell(it.mark, it.name, it.href), stateCell(it.state), h("td", { text: it.type }),
      h("td", { text: (it.detail || "").replace(/^last run /, "") || "—" })]),
    status: (it) => (it.ok ? "ok" : "attention"), statuses: [["ok", "Working"], ["attention", "Needs attention"]],
  },
  kubernetes: {
    rows: () => data.items.filter((it) => it.kind === "cluster"), empty: "No clusters set up yet",
    cols: ["Name", "Status", "Type", "Detail"],
    row: (it) => rowOf(it.href, [nameCell(it.mark, it.name, it.href), stateCell(it.state), h("td", { text: it.type }),
      h("td", { class: "home-detail", text: it.detail || "—", title: it.detail || null })]),
    status: (it) => (it.ok ? "ok" : "attention"), statuses: [["ok", "Working"], ["attention", "Needs attention"]],
  },
};

function draw() {
  const v = VIEW[S.tab];
  for (const t of TABS) {
    const b = $(`tab-${t}`);
    b.setAttribute("aria-selected", String(t === S.tab));
    b.tabIndex = t === S.tab ? 0 : -1;
  }
  $("home-panel").setAttribute("aria-labelledby", `tab-${S.tab}`);
  const all = v.rows();
  const typeOf = (x) => String(x.type || "").split(" · ")[0];
  const clouds = [...new Set(all.map((x) => x.cloud).filter(Boolean))];
  const types = [...new Set(all.map(typeOf).filter(Boolean))].sort();
  const set = (k) => (val) => { S[k] = val; saveQuery(); draw(); };
  $("home-filters").replaceChildren(window.consoleBlade.bar({ q: S.q, onQuery: (val) => { S.q = val; saveQuery(); rows(); }, pills: [
    { label: "Cloud", value: S.cloud, options: clouds.map((c) => [c, CLOUD_NAMES[c] || c]), onChange: set("cloud") },
    { label: "Type", value: S.type, options: types.map((t) => [t, t]), onChange: set("type") },
    { label: "Status", value: S.status, options: v.statuses, onChange: set("status") },
  ] }));
  $("home-head").replaceChildren(h("tr", {}, ...v.cols.map((c) => h("th", { scope: "col", text: c }))));
  rows();

  function rows() {
    for (const t of document.querySelectorAll(".home-rows-more")) t.remove();
    const q = S.q.trim().toLowerCase();
    const shown = all.filter((x) => (!S.cloud || x.cloud === S.cloud) && (!S.type || typeOf(x) === S.type)
      && (!S.status || v.status(x) === S.status)
      && (!q || [x.name, x.type, x.detail, CLOUD_NAMES[x.cloud], x.state?.[1], x.status].some((f) => String(f || "").toLowerCase().includes(q))));
    // One block per cloud, never mixed: Azure, then Google Cloud, each under its own logo and name.
    const order = ["azure", "gcp", ...new Set(shown.map((x) => x.cloud).filter((c) => !["azure", "gcp"].includes(c)))];
    const groups = order.map((c) => [c, shown.filter((x) => (x.cloud || null) === (c || null))]).filter(([, xs]) => xs.length);
    const head = (c, n) => {
      const logo = h("span", { class: "home-group-logo" });
      logo.innerHTML = window.consoleNav?.icon(c) || "";   // constant SVG only
      return h("tr", { class: "home-group" }, h("th", { scope: "colgroup", colspan: String(v.cols.length) },
        h("span", { class: "home-group-name" }, logo, CLOUD_NAMES[c] || "No cloud recorded", h("small", { text: String(n) }))));
    };
    // A cloud heading only when more than one cloud is listed: alone, it repeats the filter.
    const top = (c, n) => (groups.length > 1 ? head(c, n) : null);
    $("home-rows").replaceWith(...[h("tbody", { id: "home-rows" }, ...(groups[0] ? [top(groups[0][0], groups[0][1].length), ...groups[0][1].map(v.row)] : [])),
      ...groups.slice(1).map(([c, xs]) => h("tbody", { class: "home-rows-more" }, top(c, xs.length), ...xs.map(v.row)))]);
    $("home-count").textContent = `Showing ${shown.length} of ${all.length} records.`;
    $("home-empty").hidden = shown.length > 0;
    $("home-empty").textContent = all.length ? "Nothing matches the filters" : v.empty;
  }
}

// Essentials: label : value, two columns; a value that has a page links to it.
function essentials() {
  const wl = data.items.filter((it) => it.kind === "workload"), cl = data.items.filter((it) => it.kind === "cluster");
  const live = data.runs.filter((r) => LIVE_STATES.has(r.status)).length;
  const failed = data.runs.filter((r) => ["failed", "cancelled", "refused", "interrupted"].includes(r.status)).length;
  const last = data.runs[0];
  const pair = (label, value, href) => [h("dt", { text: label }), h("dd", {}, href ? h("a", { href, text: value }) : value)];
  const tab = (t) => `/?tab=${t}`;
  $("ess-left").replaceChildren(
    ...pair("Workloads", `${wl.length} onboarded, ${wl.filter((it) => it.ok).length} working`, tab("serverless")),
    ...pair("Clusters", `${cl.length} set up, ${cl.filter((it) => it.ok).length} working`, tab("kubernetes")));
  $("ess-right").replaceChildren(
    ...pair("Runs", `${data.runs.length} kept, ${failed} failed`, "/runs"),
    ...pair("In progress", String(live), live ? "/runs?status=active" : null),
    ...pair("Last run", last ? `${last.name} · ${runPill(last.status)[1]} · ${when(last.created)}` : "—", last?.href || null));
}

async function load() {
  $("home-count").textContent = "Loading…";
  const { items, runs, errors, signedOut } = await loadAll();
  data = { items, runs };
  $("session-state").hidden = !signedOut;
  $("session-state").textContent = "Not signed in";
  const shown = errors.filter((e) => e.message !== "signed-out");
  $("dash-errors").hidden = !shown.length;
  $("dash-errors").textContent = shown.map((e) => `${e.source}: ${e.message}`).join(" · ");
  essentials();
  draw();
}

for (const t of TABS) $(`tab-${t}`).addEventListener("click", () => {
  Object.assign(S, { tab: t, cloud: "", type: "", status: "" });
  saveQuery();
  draw();
});
$("home-panel").previousElementSibling.addEventListener("keydown", (e) => {
  const i = TABS.indexOf(S.tab), d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
  if (!d) return;
  $(`tab-${TABS[(i + d + TABS.length) % TABS.length]}`).click();
  $(`tab-${S.tab}`).focus();
});
$("ess-h").addEventListener("click", () => {
  const open = $("ess-h").getAttribute("aria-expanded") !== "true";
  $("ess-h").setAttribute("aria-expanded", String(open));
  $("ess-body").hidden = !open;
});
$("dash-refresh").addEventListener("click", () => load());
saveQuery();
draw();
load();
