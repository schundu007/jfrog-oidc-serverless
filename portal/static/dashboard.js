// Home: every cloud at a glance, read-only. One board per cloud; inside it Serverless and Kubernetes
// are separate sections, each with its own tiles and table (never mixed). Then the newest runs. Data: data.js.
import { MARK, CLOUD_NAMES, SECTIONS, LIVE_STATES, h, markEl, when, runPill, loadAll } from "./data.js";

const $ = (id) => document.getElementById(id);
const empty = (t) => h("p", { class: "muted dash-empty", text: t });
const CLOUDS = ["azure", "gcp"];   // Azure first, then Google Cloud

// A tile: the service mark, its name, its kind and count, and a status dot. One size for all.
function tile(href, markKey, name, line, dot) {
  const mark = h("span", { class: "tile-mark" });
  mark.insertAdjacentHTML("afterbegin", MARK[markKey]);  // constant SVG only
  return h("a", { class: "tile", href }, mark,
    h("span", { class: "tile-main" }, h("b", { text: name }), h("small", { text: line })),
    h("span", { class: `dot ${dot || ""}` }));
}
const ring = (id, ok, total) => {
  $(id).style.setProperty("--p", total ? String(Math.round((100 * ok) / total)) : "0");
  $(id).title = `${ok} of ${total} working`;
};
const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;

// Tables: one header row, one row per item; a row opens its page.
function table(cols, rows, emptyText) {
  if (!rows.length) return empty(emptyText);
  return h("div", { class: "table-wrap" }, h("table", { class: "list-table dash-table" },
    h("thead", {}, h("tr", {}, ...cols.map((c, i) => h("th", { scope: "col", class: i === 0 ? "col-icon" : "", text: c })))),
    h("tbody", {}, ...rows)));
}
function linkRow(href, cells) {
  const tr = h("tr", { class: href ? "row-link" : "" }, ...cells);
  if (href) tr.addEventListener("click", () => { location.href = href; });
  return tr;
}
const nameCell = (text, href) => h("th", { scope: "row" }, href ? h("a", { href, text }) : text);

function itemRow(it) {
  const [cls, text] = it.state;
  return linkRow(it.href, [h("td", { class: "col-icon" }, markEl(it.mark)), nameCell(it.name, it.href),
    h("td", { text: it.type || "" }), h("td", { class: "muted", text: it.detail || "" }), h("td", {}, h("span", { class: `pill ${cls}`, text }))]);
}

function runRow(r) {
  const [cls, text] = runPill(r.status);
  return linkRow(r.href, [h("td", { class: "col-icon" }, r.mark ? markEl(r.mark) : null), nameCell(r.name, r.href),
    h("td", { text: r.type || "" }), h("td", { class: "muted", text: when(r.created) }), h("td", {}, h("span", { class: `pill ${cls}`, text }))]);
}

// One section of a cloud: Serverless (workloads) or Kubernetes (clusters), its tiles then its table.
function groupSection(cloud, group, mine, runs) {
  const noun = group === "Kubernetes" ? "cluster" : "workload";
  const here = mine.filter((it) => (it.kind === "cluster") === (group === "Kubernetes"));
  const tiles = Object.entries(SECTIONS).filter(([, s]) => s[0] === cloud && s[4] === group).map(([key, [, label, page, mark]]) => {
    const inSec = here.filter((it) => it.section === key);
    const last = runs.find((r) => r.section === key);
    const dot = inSec.length ? (inSec.every((it) => it.ok) ? "ok" : "warn") : last ? runPill(last.status)[0] : "";
    return tile(page, mark, label, plural(inSec.length, noun), dot);
  });
  if (!tiles.length) return null;
  return h("div", { class: "board-group" },
    h("h3", { class: "board-sub" }, h("span", { text: group }), h("span", { class: "board-count", text: plural(here.length, noun) })),
    h("div", { class: "tiles" }, ...tiles),
    table(["", "Name", "Type", "Detail", "Status"], here.map(itemRow), `No ${noun}s on ${CLOUD_NAMES[cloud]} yet`));
}

// The cloud's real logo, from the sidebar (constant SVG only).
function cloudLogo(cloud) {
  const el = h("span", { class: "board-logo" });
  el.insertAdjacentHTML("afterbegin", window.consoleNav?.icon(cloud) || "");
  return el;
}

function board(cloud, items, runs) {
  const mine = items.filter((it) => it.cloud === cloud);
  const id = `h-${cloud}`;
  return h("section", { class: "board", "aria-labelledby": id },
    h("header", { class: "board-head" }, cloudLogo(cloud), h("h2", { id, text: CLOUD_NAMES[cloud] })),
    groupSection(cloud, "Serverless", mine, runs),
    groupSection(cloud, "Kubernetes", mine, runs));
}

async function load() {
  const { items, runs, errors, signedOut } = await loadAll();
  $("session-state").hidden = !signedOut;
  $("session-state").textContent = "Not signed in";
  const shown = errors.filter((e) => e.message !== "signed-out");
  $("dash-errors").hidden = !shown.length;
  $("dash-errors").textContent = shown.map((e) => `${e.source}: ${e.message}`).join(" · ");

  const workloads = items.filter((it) => it.kind === "workload"), clusters = items.filter((it) => it.kind === "cluster");
  $("hs-wl").textContent = String(workloads.length);
  $("hs-wl-ok").textContent = `${workloads.filter((it) => it.ok).length}/${workloads.length}`;
  ring("ring-wl", workloads.filter((it) => it.ok).length, workloads.length);
  $("hs-cl").textContent = String(clusters.length);
  $("hs-cl-ok").textContent = `${clusters.filter((it) => it.ok).length}/${clusters.length}`;
  ring("ring-cl", clusters.filter((it) => it.ok).length, clusters.length);
  $("hs-runs").textContent = String(runs.length);
  $("hs-live").textContent = String(runs.filter((r) => LIVE_STATES.has(r.status)).length);

  $("clouds").replaceChildren(...CLOUDS.map((c) => board(c, items, runs)));
  $("recent-runs").replaceChildren(table(["", "Name", "Type", "Started", "Status"], runs.slice(0, 8).map(runRow), "No runs yet"));
}

$("dash-refresh").addEventListener("click", load);
load();
