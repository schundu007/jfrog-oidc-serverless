// Home: every cloud at a glance, read-only. One board per cloud holds its workloads and clusters
// together (the sidebar's grouping), then the newest runs of all of them. Data: data.js.
import { MARK, CLOUD_NAMES, SECTIONS, LIVE_STATES, h, markEl, when, runPill, loadAll } from "./data.js";

const $ = (id) => document.getElementById(id);
const empty = (t) => h("p", { class: "muted dash-empty", text: t });
const CLOUDS = ["gcp", "azure"];

// A chip: the service mark, its name and count, and a status dot.
function chip(href, markKey, name, line, dot) {
  const a = h("a", { class: "chip", href }, h("span", { class: `dot ${dot || ""}` }), h("b", { text: name }), h("small", { text: line }));
  a.insertAdjacentHTML("afterbegin", MARK[markKey]);  // constant SVG only
  return a;
}
const ring = (id, ok, total) => {
  $(id).style.setProperty("--p", total ? String(Math.round((100 * ok) / total)) : "0");
  $(id).title = `${ok} of ${total} working`;
};
const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;

function itemRow(it) {
  const [cls, text] = it.state;
  return h("a", { class: "dash-row", href: it.href }, markEl(it.mark),
    h("span", { class: "dash-main" }, h("b", { text: it.name }), h("small", { text: [it.type, it.detail].filter(Boolean).join(" · ") })),
    h("span", { class: `pill ${cls}`, text }));
}

function runRow(r) {
  const [cls, text] = runPill(r.status);
  const kids = [r.mark ? markEl(r.mark) : h("span", { class: "dash-mark" }),
    h("span", { class: "dash-main" }, h("b", { text: r.name }), h("small", { text: `${r.type} · ${when(r.created)}` })),
    h("span", { class: `pill ${cls}`, text })];
  return r.href ? h("a", { class: "dash-row", href: r.href }, ...kids) : h("div", { class: "dash-row" }, ...kids);
}

function board(cloud, items, runs) {
  const mine = items.filter((it) => it.cloud === cloud);
  // Serverless, then Kubernetes: one chip per service, as the sidebar lists them.
  const groups = ["Serverless", "Kubernetes"].map((group) => {
    const chips = Object.entries(SECTIONS).filter(([, s]) => s[0] === cloud && s[4] === group).map(([key, [, label, page, mark]]) => {
      const here = mine.filter((it) => it.section === key);
      const last = runs.find((r) => r.section === key);
      const dot = here.length ? (here.every((it) => it.ok) ? "ok" : "warn") : last ? runPill(last.status)[0] : "";
      return chip(page, mark, label, plural(here.length, group === "Kubernetes" ? "cluster" : "workload"), dot);
    });
    return [h("h3", { class: "board-sub", text: group }), h("div", { class: "chips" }, ...chips)];
  });
  const id = `h-${cloud}`;
  return h("section", { class: "board", "aria-labelledby": id },
    h("header", { class: "board-head" }, h("h2", { id, text: CLOUD_NAMES[cloud] })),
    ...groups.flat(),
    h("h3", { class: "board-sub", text: "Workloads and clusters" }),
    h("div", { class: "dash-list" }, ...(mine.length ? mine.map(itemRow) : [empty(`Nothing onboarded on ${CLOUD_NAMES[cloud]} yet`)])));
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
  $("recent-runs").replaceChildren(...(runs.length ? runs.slice(0, 8).map(runRow) : [empty("No runs yet")]));
}

$("dash-refresh").addEventListener("click", load);
load();
