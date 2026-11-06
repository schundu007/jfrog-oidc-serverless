// A proof as a DAG: one column per stage (No stored secret → Live exchange → Trust removed), each check a
// node in run order, joined by edges. Clicking a node shows its status and evidence below the graph.
//
//   dag({ stages: [{ title, help?, nodes: [{ id, title, status, detail, facts? }] }], label })
//
// status is any run word (ok, done, succeeded, failed, warn, running, pending, needs_approval, skipped).

const TONE = { ok: "ok", done: "ok", succeeded: "ok", passed: "ok", failed: "bad", fail: "bad", error: "bad",
  warn: "warn", needs_approval: "wait", waiting: "wait", running: "run", pending: "idle", skipped: "idle" };
const WORD = { ok: "Passed", bad: "Failed", warn: "Warning", wait: "Waiting for approval", run: "Running", idle: "Not run" };
// Constant SVG only.
const GLYPH = {
  ok: '<path d="M4.5 8.5l2.2 2.2L11.5 6" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  bad: '<path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>',
  warn: '<path d="M8 4.5v4.2" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/><circle cx="8" cy="11.3" r="1" fill="#fff"/>',
  wait: '<path d="M8 5v3.2l2 1.3" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>',
  run: '<circle cx="8" cy="8" r="2.4" fill="#fff"/>',
  idle: "",
};

export const toneOf = (status) => TONE[String(status || "").toLowerCase()] || "idle";

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}

function dot(tone) {
  const s = el("span", { class: `dag-dot t-${tone}`, "aria-hidden": "true" });
  s.innerHTML = `<svg viewBox="0 0 16 16" width="16" height="16">${GLYPH[tone] || ""}</svg>`;   // constant SVG only
  return s;
}

// A stage passes when every node passed; it fails as soon as one failed.
function stageTone(nodes) {
  const t = nodes.map((n) => toneOf(n.status));
  if (!t.length) return "idle";
  if (t.includes("bad")) return "bad";
  if (t.includes("run") || t.includes("wait")) return "run";
  if (t.includes("warn")) return "warn";
  return t.every((x) => x === "ok") ? "ok" : "idle";
}
const STAGE_WORD = { ok: "Proven", bad: "Not proven", warn: "Warning", run: "In progress", idle: "Not run" };

// The selected node's facts: one header row, one value row.
function details(n) {
  const tone = toneOf(n.status);
  const rows = [["Status", el("span", { class: `dag-state t-${tone}` }, dot(tone), WORD[tone])], ["Check", n.title], ["Evidence", n.detail || "—"],
    ...(n.facts || []).filter(([, v]) => v != null && v !== "")];
  return el("div", { class: "table-wrap dag-detail", role: "region", "aria-live": "polite", "aria-label": `${n.title}: details` },
    el("table", { class: "kv-table" },
      el("thead", {}, el("tr", {}, rows.map(([k]) => el("th", { scope: "col", text: k })))),
      el("tbody", {}, el("tr", {}, rows.map(([k, v]) => el("td", { class: k === "Evidence" ? "dag-evidence" : null }, v))))));
}

export function dag({ stages, label = "Proof" }) {
  const all = stages.flatMap((s) => s.nodes);
  let picked = all.find((n) => toneOf(n.status) === "bad") || all.find((n) => toneOf(n.status) === "run") || all[0];
  const panel = el("div", { class: "dag-panel" });
  const buttons = new Map();
  const pick = (n) => {
    picked = n;
    for (const [m, b] of buttons) b.setAttribute("aria-pressed", String(m === n));
    panel.replaceChildren(details(n));
  };
  const graph = el("div", { class: "dag", role: "group", "aria-label": label },
    stages.map((s, i) => {
      const tone = stageTone(s.nodes);
      return el("section", { class: `dag-stage${i < stages.length - 1 ? " has-next" : ""}`, "aria-label": s.title },
        el("header", { class: "dag-head" }, el("span", { class: "dag-title", text: s.title }),
          el("span", { class: `dag-badge t-${tone}`, text: STAGE_WORD[tone] })),
        el("ol", { class: "dag-nodes" }, s.nodes.map((n) => {
          const t = toneOf(n.status);
          const b = el("button", { type: "button", class: `dag-node t-${t}`, "aria-pressed": "false", title: `${n.title}: ${WORD[t]}`,
            onclick: () => pick(n) }, dot(t), el("span", { class: "dag-node-t", text: n.title }));
          buttons.set(n, b);
          return el("li", {}, b);
        })));
    }));
  graph.style.setProperty("--dag-cols", String(stages.length));   // CSSOM: allowed under style-src 'self'
  if (picked) pick(picked);
  return el("div", { class: "dag-wrap" }, graph, panel);
}
