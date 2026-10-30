// Runs: every run of every config, newest first; select one for its request, events and log.

import { api, h, fill, state } from "./app.js";
import { $, table, pill } from "./ui.js";

let selected = null;

export function initRuns() {
  state.on("runs", render);
  state.on("config", render);
}

async function render() {
  const P = $("panel-runs");
  const { runs } = await api("/api/runs");
  const detail = h("div", { id: "run-detail" });
  fill(P, h("div", { class: "page-head" }, h("h1", {}, "Runs"), h("span", { class: "small muted" }, `${runs.length} run(s)`)),
    runs.length ? table(["created", "config", "run", "status", "changes state", ""], runs.map((r) => [r.created, r.config || "",
      `${r.kind} ${r.name}${r.request.parameters.DryRun === true ? " (DryRun)" : ""}${r.seeded ? " · recorded" : ""}`, pill(r.status),
      r.changes_state ? "yes" : "no", h("button", { class: "secondary small-btn", onclick: () => show(r.id, detail) }, "Open")]))
      : h("p", { class: "muted" }, "No runs yet."), detail);
  if (selected && runs.some((r) => r.id === selected)) show(selected, detail);
}

async function show(id, el) {
  selected = id;
  const [run, { log }] = await Promise.all([api(`/api/runs/${id}`), api(`/api/runs/${id}/log`)]);
  fill(el, h("div", { class: "card" },
    h("h2", {}, `${run.kind} ${run.name} `, pill(run.status)),
    h("p", { class: "small mono" }, `request sha256 ${run.request_sha256}`),
    run.ado_url ? h("p", { class: "small" }, `ADO run ${run.ado_run_id}: ${run.ado_url}`) : run.ado_run_id ? h("p", { class: "small" }, `ADO run ${run.ado_run_id}`) : null,
    table(["parameter", "value"], Object.entries(run.request.parameters).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v)])),
    run.guards.length ? h("p", { class: "error small" }, run.guards.join(" ")) : null,
    run.error ? h("p", { class: "error small" }, run.error) : null,
    run.result ? h("pre", { class: "code" }, JSON.stringify(run.result, null, 2)) : null,
    h("h3", {}, "Events"), h("ol", { class: "events small" }, (run.events || []).map((e) => h("li", {}, h("span", { class: "mono muted" }, `${e.t.slice(11, 19)} `), e.text))),
    log ? h("details", { class: "evidence", open: true }, h("summary", {}, `Pipeline log (${log.length.toLocaleString()} characters, redacted)`), h("pre", {}, log)) : null));
}
