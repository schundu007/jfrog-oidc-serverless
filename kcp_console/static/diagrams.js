// SVG diagrams for the Overview, drawn from /api/knowledge. Colours come from CSS classes
// (the CSP forbids inline style), text is set with textContent.

const NS = "http://www.w3.org/2000/svg";

export function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const k of kids.flat()) if (k) el.append(typeof k === "string" ? document.createTextNode(k) : k);
  return el;
}

function svgRoot(w, hgt, label) {
  return s("svg", { class: "dg", viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": label },
    s("defs", {}, s("marker", { id: "arrowhead", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7",
      markerHeight: "7", orient: "auto-start-reverse" }, s("path", { d: "M0,0 L10,5 L0,10 z", class: "arrowhead" }))));
}

// Word-wrap into <tspan>s of at most `max` characters.
function wrapText(x, y, text, max, cls, lineH = 14, anchor = "middle") {
  const words = String(text).split(/\s+/);
  const lines = [];
  let cur = "";
  for (const w of words) {
    if ((cur + " " + w).trim().length > max && cur) { lines.push(cur); cur = w; } else cur = (cur + " " + w).trim();
  }
  if (cur) lines.push(cur);
  const t = s("text", { x, y, class: cls || null, "text-anchor": anchor });
  lines.forEach((l, i) => t.append(s("tspan", { x, dy: i ? lineH : 0 }, l)));
  return { el: t, lines: lines.length };
}

function box(x, y, w, hgt, text, cls = "", max = 26) {
  const g = s("g");
  g.append(s("rect", { x, y, width: w, height: hgt, rx: 8, class: `box ${cls}` }));
  const t = wrapText(x + w / 2, 0, text, max);
  t.el.setAttribute("y", y + hgt / 2 - ((t.lines - 1) * 14) / 2 + 4);
  g.append(t.el);
  return g;
}

const arrow = (x1, y1, x2, y2) => s("line", { x1, y1, x2, y2, class: "arrow" });

export function beforeAfter(d) {
  const W = 560, bw = 160, bh = 54, gap = 30;
  const root = svgRoot(W, 230, "Before: a static credential in a Secret. After: a projected token, nothing stored.");
  [["before", 30, "bad"], ["after", 140, "good"]].forEach(([k, y, cls]) => {
    root.append(s("text", { x: 10, y: y - 10, class: "cap" }, d[k].label));
    d[k].chain.forEach((c, i) => {
      const x = 10 + i * (bw + gap);
      root.append(box(x, y, bw, bh, c, i === d[k].chain.length - 1 ? "" : cls, 24));
      if (i) root.append(arrow(x - gap + 2, y + bh / 2, x - 2, y + bh / 2));
    });
  });
  return root;
}

export function optionBFlow(f) {
  // One column, top to bottom: each step a box, the exchange (step 4) highlighted.
  const W = 560, bw = 330, bh = 36, gap = 14, x = (W - bw) / 2;
  const root = svgRoot(W, f.steps.length * (bh + gap) + 4, "Option B: pod, kubelet, plugin, Artifactory Access, registry pull");
  f.steps.forEach((st, i) => {
    const y = 2 + i * (bh + gap);
    root.append(box(x, y, bw, bh, `${i + 1}. ${st}`, i === 3 ? "hl" : "", 52));
    if (i) root.append(arrow(W / 2, y - gap + 1, W / 2, y - 1));
  });
  const y3 = 2 + 3 * (bh + gap);
  root.append(s("text", { x: x + bw + 10, y: y3 - gap / 2 + 4, class: "sub" }, "SA token →"));
  root.append(s("text", { x: x + bw + 10, y: y3 - gap / 2 + 18, class: "sub" }, "JFrog token"));
  return root;
}

export function nodeDiagram(nd) {
  const W = 560;
  const root = svgRoot(W, 300, "One AKS node: kubelet, its provider config and the plugin binaries");
  root.append(s("rect", { x: 4, y: 4, width: W - 8, height: 292, rx: 10, class: "frame" }));
  root.append(s("text", { x: 16, y: 24, class: "cap" }, "AKS node"));
  root.append(box(20, 36, 520, 44, "kubelet --image-credential-provider-config --image-credential-provider-bin-dir", "", 90));
  const col = [[20, nd.config_file, nd.providers.map((p) => `${p.name} → ${p.matchImages.join(", ")}`)],
               [290, nd.bin_dir, nd.binaries.map((b) => b.name)]];
  col.forEach(([x, cap, items]) => {
    root.append(arrow(x + 125, 82, x + 125, 104));
    root.append(s("text", { x, y: 120, class: "cap" }, cap));
    items.forEach((t, i) => root.append(box(x, 130 + i * 52, 250, 42, t, i ? "hl" : "", 40)));
  });
  root.append(s("text", { x: 20, y: 256, class: "sub" }, nd.binaries[1].note));
  root.append(s("text", { x: 20, y: 276, class: "sub" }, `Source: ${nd.source}`));
  return root;
}

export function pipeline(steps) {
  const bh = 40, W = 560, gap = 14;
  const root = svgRoot(W, steps.length * (bh + gap) + 10, "The pilot pipeline scenarios in the order they are used");
  steps.forEach((st, i) => {
    const y = 6 + i * (bh + gap);
    root.append(s("rect", { x: 6, y, width: 190, height: bh, rx: 6, class: "box hl" }));
    root.append(s("text", { x: 101, y: y + 24, "text-anchor": "middle", class: "cap" }, st.scenario));
    const t = wrapText(210, y + 11, st.text, 64, "sub", 12, "start");
    root.append(t.el);
    if (i) root.append(arrow(101, y - gap + 1, 101, y - 1));
  });
  return root;
}
