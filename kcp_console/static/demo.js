// Demo: demo mode, the rehearsal checklist (saved per config) and the demo steps as runs.

import { api, h, fill, state } from "./app.js";
import { $, needConfig, stepList, pageHead } from "./ui.js";

export function initDemo() {
  state.on("config", render);
}

async function render() {
  const P = $("panel-demo");
  if (needConfig(P)) return;
  const d = state.knowledge.DEMO;
  const name = encodeURIComponent(state.config);
  const [{ config }, { status }] = await Promise.all([api(`/api/configs/${name}`), api(`/api/status/${name}`)]);
  const ticks = status.demo || {};
  const ready = d.rehearsal.filter((r) => r.required).every((r) => ticks[r.id]);
  const mode = h("input", { type: "checkbox", checked: !!config.demo_mode, onchange: async (e) => {
    config.demo_mode = e.target.checked;
    await api(`/api/configs/${name}`, { method: "PUT", body: { config } });
    state.emit("config", state.config);
  } });
  fill(P,
    pageHead("Demo", h("label", { class: "check" }, mode, " Demo mode")),
    h("h2", {}, "Rehearsal ", h("span", { class: `pill ${ready ? "pass" : "warn"}` }, ready ? "ready" : "not ready")),
    d.rehearsal.map((r) => h("label", { class: "check" },
      h("input", { type: "checkbox", checked: !!ticks[r.id], onchange: async (e) => {
        await api(`/api/status/${name}`, { method: "PUT", body: { status: { demo: { [r.id]: e.target.checked } } } });
        render();
      } }), ` ${r.text}`, r.required ? "" : h("span", { class: "small muted" }, " (optional)"))),
    h("h2", {}, "Steps"),
    await stepList(d.run_of_show.map((s) => ({ title: s.title, line: s.say, kind: "pilot", name: s.scenario || null }))));
}
