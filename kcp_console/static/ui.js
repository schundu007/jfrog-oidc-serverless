// Shared UI pieces for every page: the status icon set, time and duration text, the "⋯" menu, the
// confirm-on-second-click pattern, check lists, step pips and the text filter for the screen.

import { h } from "./app.js";

export const $ = (id) => document.getElementById(id);
export const muted = (t) => h("span", { class: "muted" }, t);

// Short, non-identifying text for the screen: no URLs, IDs or image references, whatever a step or
// an older run recorded.
export const brief = (text) => String(text ?? "")
  .replace(/https?:\/\/\S+/g, "…")
  .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "…")
  .replace(/"?[\w.-]+\.[a-z]{2,}\/[\w./-]+(:[\w.-]+)?"?/gi, "the image")
  .replace(/\s*\(\s*…\s*\)/g, "")
  .slice(0, 160);

// ----------------------------------------------------------------- status icons

// One set everywhere (docs/ui-pattern.md): a filled circle with a glyph. Every run, step and check
// status maps to one of these kinds.
const KIND = {
  succeeded: "succeeded", ok: "succeeded", done: "succeeded", pass: "succeeded",
  failed: "failed", fail: "failed", error: "failed", refused: "failed", interrupted: "failed",
  running: "running", queued: "waiting", prepared: "waiting", waiting: "waiting",
  cancelled: "cancelled", skipped: "skipped", warn: "warning", warning: "warning",
};
export const kindOf = (status) => KIND[String(status || "").toLowerCase()] || "notrun";
export const KIND_TEXT = { succeeded: "Succeeded", failed: "Failed", running: "Running", waiting: "Waiting",
  cancelled: "Cancelled", skipped: "Skipped", warning: "Warning", notrun: "Not run" };

const SVG = "http://www.w3.org/2000/svg";
const GLYPH = {
  succeeded: "M4.6 8.3l2.2 2.2 4.6-4.8",
  failed: "M5.5 5.5l5 5M10.5 5.5l-5 5",
  waiting: "M8 4.8V8l2.2 1.4",
  cancelled: "M5.2 10.8l5.6-5.6",
  skipped: "M5 8h6",
  warning: "M8 4.6v4.2M8 11.2v.2",
};
function svg(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
// status: a run/step/check status or a kind. label: the accessible name (default: the kind's text).
export function icon(status, label) {
  const kind = KIND_TEXT[status] ? status : kindOf(status);
  const s = svg("svg", { class: `si si-${kind}`, viewBox: "0 0 16 16", role: "img", "aria-label": label || KIND_TEXT[kind] });
  if (kind === "running") {
    s.append(svg("circle", { class: "si-track", cx: "8", cy: "8", r: "6" }), svg("path", { class: "si-arc", d: "M8 2a6 6 0 0 1 6 6" }));
  } else {
    s.append(svg("circle", { class: "si-disc", cx: "8", cy: "8", r: kind === "notrun" ? "6.25" : "7.5" }));
    if (GLYPH[kind]) s.append(svg("path", { class: "si-glyph", d: GLYPH[kind] }));
  }
  return s;
}

// One small segment per step, coloured by its status.
export function pips(statuses) {
  if (!statuses.length) return null;
  return h("span", { class: "pips", role: "img", "aria-label": statuses.map((s) => KIND_TEXT[kindOf(s)]).join(", ") },
    statuses.map((s) => h("span", { class: `pip pip-${kindOf(s)}` })));
}

// Steps or checks as a list with a status icon: {status, title, detail}.
export function checklist(items) {
  return h("ul", { class: "checks" }, items.map((x) => {
    const kind = kindOf(x.status);
    return h("li", { class: `ck-${kind}` }, icon(kind), h("span", { class: "t" }, x.title || x.id), h("span", { class: "d" }, brief(x.detail)));
  }));
}

// ----------------------------------------------------------------- time

export function ago(iso) {
  if (!iso) return "";
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}

export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// "1 m 32 s" between two timestamps (to: now when absent).
export function duration(from, to) {
  if (!from) return "";
  const s = Math.max(0, Math.round(((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000));
  if (Number.isNaN(s)) return "";
  if (s < 60) return s < 1 ? "<1 s" : `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} m ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} m`;
}

// ----------------------------------------------------------------- run facts

export const TITLES = { setup: "Set up", verify: "Verify", teardown: "Tear down" };
export const ACTIVE = ["queued", "running"];
export const isDry = (r) => r.request?.parameters?.DryRun !== false;
export const runTitle = (r) => `${TITLES[r.name] || r.name}${isDry(r) ? " · dry run" : ""}`;
export const runStarted = (r) => r.approved?.at || "";
// A run's step statuses: what ran, then the plan's steps it did not reach.
export function stepStatuses(r) {
  const done = (r.result || {}).steps || [];
  const plan = r.request?.parameters?.steps || [];
  const by = Object.fromEntries(done.map((s) => [s.id, s.status]));
  const live = ACTIVE.includes(r.status) ? liveSteps(r) : {};
  return plan.length ? plan.map((s) => by[s.id] || live[s.id] || "") : done.map((s) => s.status);
}
// Step status from the run's events while it runs: ▶ title starts a step, ✓ ✗ • end it.
export function liveSteps(r) {
  const out = {};
  const plan = r.request?.parameters?.steps || [];
  for (const e of r.events || []) {
    const m = /^([▶✓✗•■]) (.*)$/.exec(e.text || "");
    const st = m && plan.find((s) => m[2] === s.title || m[2].startsWith(`${s.title}: `));
    if (st) out[st.id] = m[1] === "▶" ? "running" : m[1] === "✓" ? "ok" : m[1] === "✗" ? "failed" : "skipped";
  }
  return out;
}

// ----------------------------------------------------------------- confirm and menus

// A destructive button: the first click arms it ("Click again to …"), the second runs it.
export function confirmed(label, run, { cls = "", armedText } = {}) {
  const b = h("button", { type: "button", class: cls }, label);
  let t = null;
  b.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (b.dataset.armed !== "1") {
      b.dataset.armed = "1";
      b.textContent = armedText || `Click again to ${label.toLowerCase()}`;
      t = setTimeout(() => { if (b.isConnected) { b.dataset.armed = ""; b.textContent = label; } }, 4000);
      return;
    }
    clearTimeout(t);
    b.dataset.armed = "";
    b.disabled = true;
    try { await run(b); } finally { if (b.isConnected) b.disabled = false; }
  });
  return b;
}

// A "⋯" button with a menu. items: [{label, onclick, danger, disabled}] (danger: confirm with a second click).
// The menu is fixed-positioned under its button, so table cells and cards never clip it.
let openMenu = null;
function closeMenu() {
  if (!openMenu) return;
  openMenu.pop.remove();
  openMenu.btn.setAttribute("aria-expanded", "false");
  openMenu = null;
}
document.addEventListener("click", (e) => { if (openMenu && !openMenu.pop.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openMenu) { const b = openMenu.btn; closeMenu(); b.focus(); } });
window.addEventListener("resize", closeMenu);
window.addEventListener("scroll", closeMenu, true);

export function moreMenu(items, label = "More actions") {
  const btn = h("button", { type: "button", class: "ghost more", "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": label, title: label }, "⋯");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const was = openMenu?.btn === btn;
    closeMenu();
    if (was) return;
    const pop = h("div", { class: "menu", role: "menu" }, items.filter(Boolean).map((it) => {
      const done = async (b) => { await it.onclick(b); closeMenu(); };
      const b = it.danger ? confirmed(it.label, done, { cls: "menu-item danger" })
        : h("button", { type: "button", class: "menu-item", onclick: (ev) => { ev.stopPropagation(); closeMenu(); it.onclick(); } }, it.label);
      b.setAttribute("role", "menuitem");
      if (it.disabled) b.disabled = true;
      return b;
    }));
    pop.addEventListener("click", (ev) => ev.stopPropagation());
    pop.addEventListener("keydown", (ev) => {
      const all = [...pop.querySelectorAll(".menu-item:not(:disabled)")];
      const i = all.indexOf(document.activeElement);
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        all[(i + (ev.key === "ArrowDown" ? 1 : all.length - 1)) % all.length]?.focus();
      }
    });
    document.body.append(pop);
    const r = btn.getBoundingClientRect();
    const w = pop.offsetWidth;
    pop.style.top = `${Math.round(r.bottom + 4)}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(r.left, innerWidth - w - 8)))}px`;
    btn.setAttribute("aria-expanded", "true");
    openMenu = { btn, pop };
    pop.querySelector(".menu-item:not(:disabled)")?.focus();
  });
  return btn;
}

// A link inside the console: a plain click goes through the router, a modified one opens a tab.
export function link(go, url, path, ...kids) {
  return h("a", { href: url(path), onclick: (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    e.stopPropagation();
    go(path);
  } }, ...kids);
}
