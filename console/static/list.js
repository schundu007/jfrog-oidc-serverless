// Workloads (/<cloud>): every saved configuration of this cloud with its last run.
// The whole row opens the workload; "⋯" holds Delete.

import { $, api, h, fill, ofCloud, href, statusIcon, statusText, template, menu, ago, dur, runDuration, stamp,
  status as setStatus } from "./core.js";
import { kindLabel } from "./configure.js";

const ls = { rows: [], schema: null };

// "succeeded · 2 h ago · 1 m 32 s"
function lastRunText(run, snap) {
  if (!run) return "No runs";
  const took = dur(runDuration(snap));
  return [statusText(run.status), ago(run.created), took].filter(Boolean).join(" · ");
}

function row(c) {
  const open = href.workload(c.name);
  const tr = h("tr", { class: "row-link", "data-name": c.name },
    h("td", { class: "col-icon" }, statusIcon(c.run ? c.run.status : "notrun", c.run ? `Last run ${statusText(c.run.status)}` : "No runs")),
    h("th", { scope: "row" }, h("a", { href: open, text: c.name })),
    h("td", { text: kindLabel(ls.schema, c.kind) }),
    h("td", { class: "mono small", text: c.workload || "—" }),
    h("td", {}, c.identity ? h("span", { class: "mono small", text: c.identity }) : h("span", { class: "muted small", text: "not discovered" })),
    h("td", { class: "small", title: c.run ? stamp(c.run.created) : null, text: lastRunText(c.run, c.snap) }),
    h("td", { class: "col-menu" }, menu(`Actions for ${c.name}`, [
      { text: "Delete", danger: true, confirm: `Delete ${c.name} and its runs?`, onSelect: () => remove(c.name) },
    ])));
  tr.addEventListener("click", (e) => {
    if (e.target.closest("a, button, .menu-wrap")) return;
    location.assign(open);
  });
  return tr;
}

function render() {
  const q = $("wl-filter").value.trim().toLowerCase();
  const shown = ls.rows.filter((c) => !q || [c.name, kindLabel(ls.schema, c.kind), c.workload, c.identity]
    .some((v) => String(v || "").toLowerCase().includes(q)));
  fill($("wl-rows"), shown.map(row));
  $("wl-none").hidden = shown.length > 0 || !ls.rows.length;
  $("wl-count").textContent = ls.rows.length || "";
  $("wl-body").hidden = !ls.rows.length;
  $("wl-empty").hidden = !!ls.rows.length;
  // Empty: the empty state carries New workload.
  $("wl-new").hidden = !ls.rows.length;
}

async function load() {
  const [{ configs }, { runs }] = await Promise.all([api("/api/configs"), api("/api/runs")]);
  const mine = runs.filter((r) => ofCloud(r.kind));
  ls.rows = configs.filter((c) => ofCloud(c.kind)).map((c) => ({ ...c, run: mine.find((r) => r.name === c.name) || null, snap: null }));
  render();
  // Durations need each last run's steps.
  await Promise.all(ls.rows.filter((c) => c.run).map(async (c) => {
    try { c.snap = await api(`/api/runs/${encodeURIComponent(c.run.id)}`); } catch { /* the row keeps status and time */ }
  }));
  render();
}

async function remove(name) {
  try {
    const res = await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
    const kept = res.runs_kept ? `; ${res.runs_kept} active run(s) kept until they finish` : "";
    setStatus($("list-status"), `Deleted ${name} and ${res.runs_deleted} run(s)${kept}`, "ok");
  } catch (e) {
    setStatus($("list-status"), e.message, "error");
  }
  await load();
}

export async function mountList(page, { schema }) {
  ls.schema = schema;
  page.replaceChildren(template("tpl-list"));
  $("wl-new").setAttribute("href", href.create());
  $("wl-empty-new").setAttribute("href", href.create());
  $("wl-filter").addEventListener("input", render);
  try {
    await load();
  } catch (e) {
    $("wl-new").hidden = false;
    setStatus($("list-status"), `Could not list workloads: ${e.message}`, "error");
  }
}
