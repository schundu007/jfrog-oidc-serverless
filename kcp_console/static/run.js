// One run at /clusters/<cloud>/<name>/runs/<id>: a header (what ran, its status, started · duration,
// Cancel while active), then a step tree on the left and the selected step's detail and log on the right.
// A run waiting for approval shows its approval at the top of the right pane. ?step=<id> keeps the selection.

import { api, h, fill, state, refreshConfigs, route, go, url, clusterPath } from "./app.js";
import { $, muted, brief, icon, kindOf, KIND_TEXT, when, duration, link, liveSteps, ACTIVE, runTitle, runStarted } from "./ui.js";
import { approvalBox, dryBefore } from "./cluster.js";

const P = () => $("panel-run");
const here = () => { const r = route(); return r.page === "run" ? r : null; };
const PILL = { succeeded: "ok", failed: "fail", running: "info", waiting: "warn" };
const ROOT = "run";

let run = null, dry = null, extra = "", error = "", timer = null, loading = null;

export function initRun() {
  state.on("page", (r) => {
    if (r.page !== "run") return;
    if (state.config !== r.name && state.configs.some((c) => c.name === r.name)) refreshConfigs(r.name);
    if (run?.id !== r.run) { run = null; dry = null; extra = ""; error = ""; }
    load(r.run);
  });
}

async function load(id) {
  if (loading === id) return;
  loading = id;
  clearTimeout(timer);
  render();
  try {
    const r = await api(`/api/runs/${id}`);
    if (here()?.run !== id) return;
    run = r;
    error = "";
    if (r.status === "prepared" && !dry) {
      const { runs } = await api("/api/runs");
      dry = dryBefore(runs.filter((x) => x.config === r.config), r);
    }
    // Pipeline runs keep their full log apart from the events.
    if (r.kind !== "direct" && !ACTIVE.includes(r.status) && !extra) extra = (await api(`/api/runs/${id}/log`)).log || "";
  } catch (err) {
    error = err.message;
  } finally {
    loading = null;
  }
  render();
  if (run && ACTIVE.includes(run.status)) timer = setTimeout(() => { if (here()?.run === id) load(id); }, 2000);
}

// ----------------------------------------------------------------------- steps and their logs

// The tree: the whole run first, then every plan step with its status, duration and log lines.
function steps(r) {
  const plan = r.request?.parameters?.steps || [];
  const done = Object.fromEntries((((r.result || {}).steps) || []).map((s) => [s.id, s]));
  const live = liveSteps(r);
  const evs = r.events || [];
  const rows = plan.map((s) => ({ id: s.id, title: s.title, plan: s, status: done[s.id]?.status || live[s.id] || "", detail: done[s.id]?.detail || "", lines: [] }));
  let cur = null;
  for (const e of evs) {
    const m = /^([▶✓✗•■]) (.*)$/.exec(e.text || "");
    const st = m && rows.find((s) => m[2] === s.title || m[2].startsWith(`${s.title}: `));
    if (m && m[1] === "▶" && st) { cur = st; st.from = e.t; }
    if (cur) cur.lines.push(e);
    if (m && m[1] !== "▶" && st) { st.to = e.t; if (st === cur) cur = null; }
  }
  // A step that started and never ended: still running (no end yet), or cut off (no duration).
  for (const s of rows) if (s.from && !s.to && !ACTIVE.includes(r.status)) s.from = "";
  const root = { id: ROOT, title: runTitle(r), status: r.status, detail: r.error || "", lines: evs, from: r.finished || ACTIVE.includes(r.status) ? runStarted(r) : "", to: r.finished, root: true };
  return [root, ...rows];
}

function selected(list) {
  const want = new URLSearchParams(location.search).get("step");
  return list.find((s) => s.id === want)
    || list.find((s) => !s.root && kindOf(s.status) === "running")
    || list.find((s) => !s.root && kindOf(s.status) === "failed")
    || list[0];
}

function choose(id) {
  const u = new URLSearchParams(location.search);
  if (id === ROOT) u.delete("step"); else u.set("step", id);
  history.replaceState({}, "", `${location.pathname}${u.toString() ? `?${u}` : ""}`);
  render(true);
}

// ----------------------------------------------------------------------- page

function render(focusTree = false) {
  const r = here();
  if (!r) return;
  if (!run) return fill(P(), error ? h("div", { class: "empty" }, h("p", {}, brief(error)), backLink(r)) : muted("Loading…"));
  const list = steps(run);
  const sel = selected(list);
  const kind = kindOf(run.status);
  const started = runStarted(run);
  const active = ACTIVE.includes(run.status) || run.status === "prepared";
  fill(P(), h("div", { class: "page" },
    h("header", { class: "page-head" },
      h("div", { class: "page-id" },
        h("div", { class: "page-title" }, icon(run.status), h("h1", {}, runTitle(run)),
          h("span", { class: `badge ${PILL[kind] || ""}` }, run.status === "prepared" ? "Waiting for approval" : KIND_TEXT[kind])),
        h("p", { class: "facts" }, `Run ${run.id} · `, started ? [`started ${when(started)}`, run.finished || ACTIVE.includes(run.status) ? duration(started, run.finished) : run.status].join(" · ") : `prepared ${when(run.created)}`,
          " · ", backLink(r))),
      active ? h("div", { class: "head-actions" }, h("button", { class: "secondary", onclick: cancel }, "Cancel")) : null),
    error ? h("div", { class: "callout fail" }, brief(error)) : null,
    h("div", { class: "run-grid" }, tree(list, sel), pane(r, sel))));
  if (focusTree) P().querySelector(".step[aria-selected=\"true\"]")?.focus();
}

// Local time of day, like every other time on the page.
const clock = (iso) => { const d = new Date(iso || ""); return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour12: false }); };

const backLink = (r) => link(go, url, clusterPath(r.name, "runs"), r.name);

function tree(list, sel) {
  const ul = h("ul", { class: "step-tree", role: "listbox", "aria-label": "Steps" }, list.map((s) => h("li", {
    class: `step${s.root ? " root" : ""}`, role: "option", tabindex: s === sel ? "0" : "-1", "aria-selected": s === sel ? "true" : "false",
    onclick: () => choose(s.id),
  }, icon(s.status), h("span", { class: "t" }, s.title), h("span", { class: "dur" }, s.from ? duration(s.from, s.to || undefined) : ""))));
  ul.addEventListener("keydown", (e) => {
    const i = list.indexOf(sel);
    const j = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: list.length - 1 }[e.key];
    if (j === undefined) return;
    e.preventDefault();
    if (list[j]) choose(list[j].id);
  });
  return h("nav", { class: "card tree-card", "aria-label": "Run steps" }, ul);
}

// The log shows what happened, not how: command lines ("$ …") and HTTP calls ("GET <url>") stay
// in the stored run, off the screen and out of Copy.
const COMMAND = /^\s*(\$ |(GET|POST|PUT|PATCH|DELETE|HEAD) https?:\/\/)/;

function pane(r, sel) {
  const lines = sel.lines.filter((e) => !COMMAND.test(e.text || "")).map((e) => `${clock(e.t)}  ${e.text}`);
  if (sel.root && extra) lines.push(...extra.split("\n").filter((t) => !COMMAND.test(t)));
  const copy = h("button", { class: "ghost", onclick: async () => {
    try { await navigator.clipboard.writeText(lines.join("\n")); copy.textContent = "Copied"; } catch { copy.textContent = "Copy failed"; }
    setTimeout(() => { if (copy.isConnected) copy.textContent = "Copy"; }, 2000);
  } }, "Copy");
  const facts = sel.root
    ? [["Action", runTitle(run)], ["Cluster", run.cluster], ["Prepared", when(run.created)], ["Approved", when(run.approved?.at) || "—"],
      ["Finished", when(run.finished) || "—"], ["Request", h("code", {}, (run.request_sha256 || "").slice(0, 12))]]
    : [["Status", KIND_TEXT[kindOf(sel.status)]], ["Duration", sel.from ? duration(sel.from, sel.to || undefined) : "—"],
      ["Kind", sel.plan.kind || "—"], sel.plan.dry_run_note ? ["Dry run", sel.plan.dry_run_note] : null];
  return h("div", { class: "run-pane" },
    run.status === "prepared" ? h("section", { class: "card needs-you" },
      h("div", { class: "card-head" }, icon("waiting"), h("h2", {}, "Approval needed"), muted(runTitle(run))),
      approvalBox(r.name, run, dry, () => { dry = null; load(run.id); })) : null,
    h("section", { class: "card" },
      h("div", { class: "card-head" }, icon(sel.status), h("h2", {}, sel.title), muted(KIND_TEXT[kindOf(sel.status)])),
      sel.detail ? h("p", { class: "step-detail" }, brief(sel.detail)) : null,
      (run.guards || []).length && sel.root ? h("div", { class: "callout fail" }, brief(run.guards.join(" "))) : null,
      h("table", { class: "kv-table" }, h("thead", {}, h("tr", {}, facts.filter(Boolean).map(([k]) => h("th", { scope: "col" }, k)))), h("tbody", {}, h("tr", {}, facts.filter(Boolean).map(([, v]) => h("td", {}, v)))))),
    h("section", { class: "card log-card" },
      h("div", { class: "card-head" }, h("h2", {}, "Log"), muted(`${lines.length} line${lines.length === 1 ? "" : "s"}`), copy),
      lines.length ? h("ol", { class: "log" }, lines.map((t) => h("li", {}, t))) : muted(run.status === "prepared" ? "Starts after the approval." : "No output.")));
}

async function cancel() {
  try { await api(`/api/runs/${run.id}/cancel`, { method: "POST", body: {} }); } catch (err) { error = err.message; }
  load(run.id);
}
