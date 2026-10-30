// Troubleshoot: the rule engine over this config's runs (and anything pasted), ranked, with the
// next check as a run control.

import { api, h, fill, state } from "./app.js";
import { $, findingView, needConfig, pageHead } from "./ui.js";

export function initTroubleshoot() {
  state.on("config", () => render());
  state.on("runs", () => render());
}

async function render(text) {
  const P = $("panel-troubleshoot");
  if (needConfig(P)) return;
  const box = h("textarea", { rows: "5", class: "paste", spellcheck: "false",
    placeholder: "Optional: paste node-check output, pod events or a plugin log (analysed, not stored)" });
  if (text) box.value = text;
  let a;
  try { a = await api("/api/diagnose", { method: "POST", body: { config: state.config, text: text || "" } }); }
  catch (e) { fill(P, h("p", { class: "error" }, e.message)); return; }
  fill(P,
    pageHead("Troubleshoot", `Diagnosis from ${a.runs_used.length} run(s)${text ? " and the pasted text" : ""}.`,
      h("button", { class: "primary", onclick: () => render(box.value) }, "Analyse")),
    box,
    a.findings.length ? a.findings.map((f, i) => findingView(f, f.rule === a.verdict.root_cause || (i === 0 && f.level === "cause")))
      : h("p", { class: "outcome" }, "No findings."));
}
