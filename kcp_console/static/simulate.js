// Simulate: run a scenario, then show the verdict, the causal chain, a swim-lane timeline,
// the ranked diagnosis, the three-layer diff and the raw evidence the rules read.

import { h, fill, rich } from "./app.js";
import { s } from "./diagrams.js";
import { layersView } from "./ui.js";

const $ = (id) => document.getElementById(id);
const LANES = ["pipeline", "writes", "kubelet", "plugin", "token-cache", "image-cache", "pull", "artifactory"];
const LANE_LABEL = { pipeline: "pipeline", writes: "node file", kubelet: "kubelet", plugin: "plugin",
  "token-cache": "token cache", "image-cache": "image cache", pull: "pod pull", artifactory: "Artifactory" };

let API;
let current = null;

export async function initSimulate({ api }) {
  API = api;
  const { scenarios } = await api("/api/sim/scenarios");
  fill($("sim-list"), scenarios.map((sc) => h("li", {}, h("button", {
    "data-name": sc.name, title: sc.story, onclick: () => run(sc.name),
  }, h("span", {}, sc.title), h("span", { class: "exp" }, sc.expect.verdict || "")))));
}

async function run(name) {
  current = name;
  document.querySelectorAll("#sim-list button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.name === name)));
  let r;
  try {
    r = await API("/api/sim", { method: "POST", body: { scenario: name } });
  } catch (err) {
    $("sim-empty").hidden = false;
    $("sim-empty").textContent = err.message;
    return;
  }
  if (current !== name) return;
  render(r);
}

function render(r) {
  $("sim-empty").hidden = true;
  $("sim-result").hidden = false;
  $("sim-title").textContent = r.title;
  const v = $("sim-verdict");
  v.textContent = r.verdict;
  v.className = `pill ${r.verdict.toLowerCase()}`;
  $("sim-story").textContent = r.story;
  $("sim-runbook").textContent = `Runbook: ${r.runbook}`;
  const top = r.diagnosis.findings.find((f) => f.rule === r.root_cause);
  const root = $("sim-root");
  root.className = `callout ${r.verdict === "PASS" ? "ok" : r.verdict === "FAIL" ? "fail" : "warn"}`;
  fill(root, r.root_cause
    ? [h("strong", {}, "Root cause: "), top ? top.title : r.root_cause]
    : [h("strong", {}, "No fault found. "), r.diagnosis.verdict.pod?.exchange || ""]);
  $("sim-mismatch").hidden = !r.mismatches.length;
  $("sim-mismatch").textContent = r.mismatches.length ? `Differs from the scenario's expectation: ${r.mismatches.join("; ")}` : "";
  fill($("sim-chain"), r.chain.length
    ? r.chain.map((c) => { const [ts, ...rest] = c.split(" "); return h("li", {}, h("span", { class: "ts" }, ts.slice(11, 19)), rest.join(" ")); })
    : [h("li", { class: "muted" }, "Nothing went wrong along the way.")]);
  fill($("sim-timeline"), timeline(r.events));
  fill($("sim-findings"), r.diagnosis.findings.length ? r.diagnosis.findings.map((f) => finding(f, f.rule === r.root_cause))
    : [h("p", { class: "muted" }, "No findings.")]);
  fill($("sim-layers"), layersView(r.diagnosis.three_layers));
  fill($("sim-evidence"), evidence(r));
}

function finding(f, isTop) {
  return h("div", { class: `finding ${f.level}${isTop ? " top" : ""}` },
    h("div", { class: "head" }, h("strong", {}, f.title), h("span", { class: "rule" }, f.rule),
      f.label ? h("span", { class: "tag" }, f.label) : null, h("span", { class: "score" }, `score ${f.score}`)),
    f.hint ? h("p", { class: "small" }, f.hint) : null,
    f.where ? h("p", { class: "small muted" }, "Look at: ", rich(f.where)) : null,
    f.evidence.length ? h("pre", {}, f.evidence.join("\n")) : null,
    f.next ? h("p", { class: "next" }, "Next: run ", h("code", {}, f.next.scenario),
      f.next.dry_run ? " with DryRun true first" : " (read-only)") : null);
}

function evidence(r) {
  const e = r.evidence;
  const block = (title, text) => (text ? h("details", { class: "evidence" }, h("summary", {}, title), h("pre", {}, text)) : null);
  return [
    ...e.pods.map((p) => block(`Pod events: ${p.name} (${p.sa}) on ${p.node}`, p.events)),
    ...e.kubelet_checks.map((c, i) => block(`CHECK_KUBELET_PROVIDER output #${i + 1}`, c)),
    ...e.install_logs.map((l, i) => block(`INSTALL_JFROG_PROVIDER log #${i + 1}`, l)),
    ...e.reset_logs.map((l, i) => block(`RESET_NODE_JFROG_ENTRY log #${i + 1}`, l)),
    block("Committed values file", e.values_file),
    block("Artifactory audit records", e.audit.map((a) => `${a.t} ${a.method} ${a.path} → ${a.status} ${a.user || "(no user)"}`).join("\n")),
  ];
}

function timeline(events) {
  const shown = events.filter((e) => e.kind !== "wait" || true);
  const step = 22, left = 110, laneH = 26;
  const W = Math.max(720, left + shown.length * step + 30);
  const H = LANES.length * laneH + 30;
  const root = s("svg", { class: "tl", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Swim-lane timeline of the simulated events" });
  LANES.forEach((lane, i) => {
    root.append(s("rect", { x: 0, y: i * laneH + 2, width: W, height: laneH - 4, rx: 4, class: i % 2 ? "" : "lanebg", fill: i % 2 ? "none" : null }));
    root.append(s("text", { x: 8, y: i * laneH + 18, class: "lane" }, LANE_LABEL[lane]));
  });
  let lastLabel = "";
  shown.forEach((e, i) => {
    const li = LANES.indexOf(e.lane);
    if (li < 0) return;
    const x = left + i * step;
    const cls = e.ok === true ? "ev ok" : e.ok === false ? "ev bad" : "ev";
    const dot = s("circle", { cx: x, cy: li * laneH + 14, r: 6, class: cls, tabindex: "0" },
      s("title", {}, `${e.time.slice(11, 19)} ${e.node ? e.node.replace(/^aks-appnodepool-\d+-/, "") + ": " : ""}${e.detail}`));
    root.append(dot);
    const label = e.time.slice(11, 16);
    if (label !== lastLabel && i % 4 === 0) {
      root.append(s("line", { x1: x, y1: LANES.length * laneH, x2: x, y2: LANES.length * laneH + 5, class: "tick" }));
      root.append(s("text", { x: x - 14, y: H - 6 }, label));
      lastLabel = label;
    }
  });
  return root;
}
