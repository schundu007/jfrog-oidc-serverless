// Runs: every run, newest first; open one for its steps and events.

import { api, h, fill, state } from "./app.js";
import { $, table, pill, workflow } from "./ui.js";

let selected = null;

export function initRuns() {
  state.on("runs", render);
  state.on("tab", (name) => { if (name === "runs") render(); });
}

const label = (r) => `${{ setup: "Set up", verify: "Verify", teardown: "Tear down" }[r.name] || r.name}${r.request.parameters.DryRun === true ? " · dry run" : ""}`;

async function render() {
  const P = $("panel-runs");
  // Set up, Verify and Tear down that ran (dry runs included); not requests left unapproved.
  const runs = (await api("/api/runs")).runs.filter((r) => r.kind === "direct" && r.status !== "prepared");
  const detail = h("div", { id: "run-detail" });
  fill(P, h("div", { class: "cfg-head" }, h("div", {}, h("h1", {}, "Runs"))),
    runs.length ? h("table", { class: "cfg-table clickable" },
      h("thead", {}, h("tr", {}, ["When", "Cluster", "Action", "Status"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, runs.map((r) => h("tr", { class: r.id === selected ? "is-open" : "", onclick: () => show(r.id, detail) },
        h("td", { class: "small" }, r.created.slice(0, 19).replace("T", " ")), h("td", {}, r.config || ""),
        h("td", {}, h("b", {}, label(r))), h("td", {}, pill(r.status))))))
      : h("p", { class: "empty" }, "No runs yet"), detail);
  if (selected && runs.some((r) => r.id === selected)) show(selected, detail);
}

async function show(id, el) {
  selected = id;
  const run = await api(`/api/runs/${id}`);
  const steps = (run.result || {}).steps;
  fill(el, h("section", { class: "summary" },
    h("div", { class: "toolbar" }, h("h2", {}, `${run.config} · ${label(run)} `, pill(run.status))),
    run.guards.length ? h("p", { class: "error small" }, run.guards.join(" ")) : null,
    run.error ? h("p", { class: "error small" }, run.error) : null,
    steps ? table(["", "step", "detail"], steps.map((s) => [pill(s.status), s.title || s.id, s.detail])) : null,
    h("details", {}, h("summary", { class: "small" }, "Log"),
      h("ol", { class: "events small" }, (run.events || []).filter((e) => workflow(e.text)).map((e) => h("li", {}, h("span", { class: "mono muted" }, `${e.t.slice(11, 19)} `), e.text))))));
}
