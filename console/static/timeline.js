// Swim-lane SVG timeline for a simulation result (spec §7.5).
// Built with createElementNS + textContent only: no innerHTML, no inline styles (CSP).

const NS = "http://www.w3.org/2000/svg";
const LABEL_W = 132;
const PAD_R = 12;
const AXIS_H = 22;

const LANES = [
  { key: "faults", label: "Faults", h: 22 },
  { key: "requests", label: "Requests", h: 30 },
];

export const LEGEND = [
  ["sw-fault", "fault window"],
  ["sw-ok", "ok"],
  ["sw-cached", "served from cache"],
  ["sw-unavail", "unavailable (outage)"],
  ["sw-fail", "failed"],
];

function el(name, attrs = {}, parent = null, title = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (title) {
    const t = document.createElementNS(NS, "title");
    t.textContent = title;
    node.appendChild(t);
  }
  if (parent) parent.appendChild(node);
  return node;
}

export function hhmm(t) {
  const neg = t < 0;
  const s = Math.floor(Math.abs(t));
  const out = `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  return neg ? `−${out}` : out;
}

export function renderTimeline(container, result) {
  container.replaceChildren();
  const m = result.metrics || {};
  const dur = result.duration_s;
  const width = Math.max(640, container.clientWidth || 960);
  const plotW = width - LABEL_W - PAD_R;
  const height = AXIS_H + LANES.reduce((a, l) => a + l.h + 6, 0) + 4;
  const x = (t) => LABEL_W + (Math.min(Math.max(t, 0), dur) / dur) * plotW;
  const w = (a, b) => Math.max(1, x(b) - x(a));

  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`, width, height, class: "tl",
    role: "img", "aria-label": `Timeline of ${result.scenario}, ${Math.round(dur / 3600)} hours`,
  }, container);
  const defs = el("defs", {}, svg);
  const pat = el("pattern", { id: "hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" }, defs);
  el("rect", { width: 6, height: 6, class: "hatch-bg" }, pat);
  el("line", { x1: 0, y1: 0, x2: 0, y2: 6, class: "hatch-line" }, pat);

  // Axis: hour ticks, every 1/2/3/6 h depending on duration.
  const hours = dur / 3600;
  // At least ~56 px between labels so they never overlap on narrow screens.
  const step = [1, 2, 3, 6, 12, 24].find((s) => s >= (hours <= 12 ? 1 : hours <= 24 ? 2 : hours <= 48 ? 3 : 6)
    && plotW / (hours / s) >= 56) || 24;
  for (let h = 0; h <= hours; h += step) {
    const xx = x(h * 3600);
    el("line", { x1: xx, y1: AXIS_H - 4, x2: xx, y2: height, class: "grid" }, svg);
    const anchor = h === 0 ? "start" : h + step > hours ? "end" : "middle";
    const t = el("text", { x: xx, y: 12, class: "axis", "text-anchor": anchor }, svg);
    t.textContent = hhmm(h * 3600);
  }

  let y = AXIS_H;
  const lane = {};
  for (const l of LANES) {
    lane[l.key] = { y, h: l.h };
    el("rect", { x: LABEL_W, y, width: plotW, height: l.h, class: "lane-bg" }, svg);
    const lt = el("text", { x: 6, y: y + l.h / 2 + 4, class: "lane-label" }, svg);
    lt.textContent = l.label;
    y += l.h + 6;
  }

  // Fault windows: hatched in their own lane, a faint band across the whole plot.
  for (const f of m.faults || []) {
    const end = f.end ?? dur;
    const label = `${f.type} ${hhmm(f.start)}–${f.end == null ? "end" : hhmm(f.end)}`;
    el("rect", { x: x(f.start), y: AXIS_H, width: w(f.start, end), height: height - AXIS_H, class: "fault-band" }, svg);
    const L = lane.faults;
    el("rect", { x: x(f.start), y: L.y + 2, width: w(f.start, end), height: L.h - 4, class: "fault", fill: "url(#hatch)" }, svg, label);
  }

  // Requests: per-pixel-column density, coloured by the worst outcome in the column.
  const R = lane.requests;
  const cols = new Map();
  const rank = { ok: 0, ok_cached: 1, unavailable: 2, failed: 3 };
  for (const rq of m.requests || []) {
    const c = Math.floor(x(rq.t));
    const cur = cols.get(c) || { n: 0, worst: "ok", reasons: new Set() };
    cur.n += rq.n;
    if ((rank[rq.outcome] ?? 0) > rank[cur.worst]) cur.worst = rq.outcome;
    if (rq.reason) cur.reasons.add(rq.reason);
    cols.set(c, cur);
  }
  let maxN = 1;
  for (const c of cols.values()) maxN = Math.max(maxN, c.n);
  const cls = { ok: "ok", ok_cached: "cached", unavailable: "unavail", failed: "fail" };
  for (const [c, v] of cols) {
    const hh = Math.max(2, (v.n / maxN) * (R.h - 4));
    const tip = `${v.n} request(s), worst: ${v.worst}${v.reasons.size ? ` — ${[...v.reasons].join("; ")}` : ""}`;
    el("rect", { x: c, y: R.y + R.h - 2 - hh, width: 1.2, height: hh, class: `bar ${cls[v.worst] || "ok"}` }, svg, tip);
  }

  return svg;
}
