// Docs: your own runbook (.kcp-console/runbook.md, local only, never in the repo) and the
// reference the console is built on, with a contents list.

import { api, h, fill, rich, state } from "./app.js";
import { $, doc, table } from "./ui.js";
import { beforeAfter, optionBFlow, nodeDiagram, pipeline } from "./diagrams.js";

export function initDocs() {
  render();
}

// A small Markdown subset, as text nodes only: headings, fenced code, lists, tables, paragraphs.
function markdown(text) {
  const out = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("```")) {
      const code = [];
      while (++i < lines.length && !lines[i].startsWith("```")) code.push(lines[i]);
      out.push(h("pre", { class: "code" }, code.join("\n")));
    } else if (/^#{1,4} /.test(l)) {
      const n = l.match(/^#+/)[0].length;
      out.push(h(`h${Math.min(n + 1, 5)}`, {}, rich(l.slice(n + 1))));
    } else if (/^\s*\|/.test(l)) {
      const rows = [];
      for (; i < lines.length && /^\s*\|/.test(lines[i]); i++) {
        if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
      }
      i--;
      if (rows.length) out.push(table(rows[0], rows.slice(1)));
    } else if (/^\s*([-*]|\d+\.) /.test(l)) {
      const items = [];
      for (; i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i]); i++) items.push(h("li", {}, rich(lines[i].replace(/^\s*([-*]|\d+\.) /, ""))));
      i--;
      out.push(h("ul", {}, items));
    } else if (l.trim()) out.push(h("p", {}, rich(l)));
  }
  return out;
}

async function render() {
  const k = state.knowledge;
  let own = { text: null, path: ".kcp-console/runbook.md" };
  try { own = await api("/api/docs/runbook"); } catch { /* none */ }
  const sections = [
    ["Overview", [k.STORY, k.WHERE_THINGS_STAND, k.STATUS_AT_A_GLANCE]],
    ["Environment", k.ENVIRONMENT],
    ["Why changes run through a pipeline", k.EXECUTION_MODEL],
    ["Scope: Option B, Option A", k.SCOPE],
    ["Protected providers", k.PROTECTED_PROVIDERS],
    ...k.PHASES.map((p) => [`Phase ${p.id}: ${p.title}`, Object.fromEntries(Object.entries(p).filter(([x]) => !["id", "title"].includes(x)))]),
    ["Pipeline scenarios", k.PILOT_SCENARIOS.concat(k.CONSOLE_SCENARIOS)],
    ["Output rules", k.OUTPUT_RULES],
    ["Guards", k.GUARDS],
    ["Bugs and what guards them", k.BUGS],
    ["The doubled URL scheme", k.DOUBLED_HTTPS],
    ["Node migration", k.NODE_MIGRATION],
    ["Troubleshooting", k.TROUBLESHOOTING],
    ["Reading the plugin log", k.PLUGIN_LOG_TABLE],
    ["kubelet's lines", k.KUBELET_LINES],
    ["Audit attribution", k.AUDIT],
    ["Template: status", k.TEMPLATE_STATUS],
    ["Template: paths", k.TEMPLATE_PATHS],
    ["Template: inputs", k.TEMPLATE_INPUTS],
    ["Template: actions", k.TEMPLATE_ACTIONS],
    ["Template: first run", k.FIRST_RUN],
    ["The download token secret variable", k.SECRET_VARIABLE],
    ["Identity mapping rules", k.MAPPING_RULES],
    ["Large clusters", k.LARGE_CLUSTERS],
    ["Template gaps", k.TEMPLATE_GAPS],
    ["Validator rules", k.VALIDATOR_RULES],
    ["Security review", k.SECURITY_REVIEW],
    ["Demo", k.DEMO],
    ["Rollback", k.ROLLBACK],
    ["Open items", k.OPEN_ITEMS],
    ["Testing performed", k.TESTING_PERFORMED],
    ["Not yet tested", k.TESTING_NOT_PERFORMED],
    ["Sources", [k.SOURCES, k.SOURCES_CAVEAT]],
  ];
  const id = (i) => `doc-${i}`;
  const body = h("div", { class: "docs-body" },
    h("section", { id: "doc-how", class: "block" }, h("h2", {}, "How it works"),
      h("div", { class: "diagram-row" },
        h("figure", { class: "card" }, h("figcaption", {}, "Before and after"), beforeAfter(k.DIAGRAMS.before_after)),
        h("figure", { class: "card" }, h("figcaption", {}, "Option B: the token exchange"), optionBFlow(k.DIAGRAMS.option_b_flow),
          h("p", { class: "small muted" }, `${k.DIAGRAMS.option_b_flow.note} ${k.DIAGRAMS.option_b_flow.why_b}`))),
      h("div", { class: "diagram-row" },
        h("figure", { class: "card" }, h("figcaption", {}, "On each node"), nodeDiagram(k.DIAGRAMS.node),
          h("p", { class: "small muted" }, k.DIAGRAMS.node.kubelet)),
        h("figure", { class: "card" }, h("figcaption", {}, "The pipeline scenarios"), pipeline(k.PIPELINE_DIAGRAM)))),
    h("section", { id: "doc-own", class: "block" }, h("h2", {}, "Your runbook"),
      own.text ? markdown(own.text)
        : h("p", { class: "muted" }, `Put your runbook (Markdown) at ${own.path} to read it here. That folder is gitignored: it stays on this machine.`)),
    sections.map(([title, value], i) => h("section", { id: id(i), class: "block" }, h("h2", {}, title),
      Array.isArray(value) && value.length && value.every((v) => !Array.isArray(v) && typeof v !== "object") ? doc(value)
        : Array.isArray(value) && value.some((v) => Array.isArray(v) || (v && typeof v === "object")) && value.length <= 3 && value.some((v) => typeof v === "string")
          ? value.map((v) => doc(v)) : doc(value))));
  const toc = h("nav", { class: "docs-toc", "aria-label": "Contents" },
    h("a", { href: "#doc-how", onclick: (e) => jump(e, "doc-how") }, "How it works"),
    h("a", { href: "#doc-own", onclick: (e) => jump(e, "doc-own") }, "Your runbook"),
    sections.map(([title], i) => h("a", { href: `#${id(i)}`, onclick: (e) => jump(e, id(i)) }, title)));
  fill($("panel-docs"), h("div", { class: "docs-layout" }, toc, body));
}

function jump(e, target) {
  e.preventDefault();
  document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
}
