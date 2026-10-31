// Run page (/<cloud>/w/<name>/runs/<id>): the pipeline's step tree next to the selected step's
// detail (approval gates, evidence) and its log. Rendered with DOM APIs only (no innerHTML).
// Everything shown has been redacted by the server; approvals send back only the hashes it showed.

import { $, api, h, fill, streamEvents, statusIcon, statusPill, template, armed, href, ago, stamp, dur,
  runDuration, stepDuration, LIVE, status as setStatus } from "./core.js";

const rs = { id: null, name: null, snap: null, selected: null, abort: null, log: [], refetch: null, userPicked: false, tick: null };

const note = (text, kind) => setStatus($("run-status"), text, kind);

// ----------------------------------------------------------------- per-step views

function outcome(text, ok) {
  return h("p", { class: `outcome${ok ? "" : " is-warn"}` }, statusIcon(ok ? "succeeded" : "warn"), text);
}

// Facts as a table: one header row of labels, one row of values (few rows, many columns).
function kv(rows, lead = null) {
  const cols = rows.filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => [k, h("span", { class: "mono", text: typeof v === "object" ? JSON.stringify(v) : String(v) })]);
  if (lead) cols.unshift(lead);
  return h("table", { class: "kv-table" },
    h("thead", {}, h("tr", {}, cols.map(([k]) => h("th", { scope: "col", text: k })))),
    h("tbody", {}, h("tr", {}, cols.map(([, v]) => h("td", {}, v)))));
}

function viewPublish(step, ev) {
  const r = ev.result;
  if (!r) return [];
  const pr = r.pull_request;
  const link = (url, text) => h("p", {}, h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text }));
  return [
    pr ? link(pr.url, `Pull request #${pr.id}`)
      : r.pipeline ? link(r.pipeline.url, `Pushed to ${r.branch}: ${r.pipeline.name}`)
      : outcome("No change", true),
    kv([["Branch", r.branch], ["Commit", r.commit ? r.commit.slice(0, 12) : null], ["Workspace", r.workspace]]
      .filter(([, v]) => v)),
  ];
}

function viewPreflight(step, ev) {
  const r = ev.result;
  if (!r) return [];
  return [h("ul", { class: "checklist" }, r.checks.map((c) => h("li", { class: `chk chk-${c.status}` }, statusIcon(c.status),
    h("div", {}, h("b", { text: c.title }), c.status !== "ok" && c.detail ? h("div", { class: "small mono", text: c.detail }) : null,
      c.fix && c.status !== "ok" ? h("div", { class: "fix small", text: c.fix }) : null))))];
}

function viewDiscover(step, ev) {
  const r = ev.result;
  if (!r) return [];
  const out = [];
  if (r.corrections?.length) {
    out.push(h("div", { class: "corrections" }, h("b", { text: "Corrected:" }),
      h("ul", {}, r.corrections.map((c) => h("li", { text: `${c.field}: ${c.from} → ${c.to} (${c.reason})` })))));
  }
  for (const w of [...(r.errors || []), ...(r.warnings || [])]) {
    out.push(h("p", { class: "small" }, h("b", { text: w.message }), w.fix ? h("span", { class: "fix", text: ` — ${w.fix}` }) : null));
  }
  if (!out.length) out.push(outcome("Identity found", true));
  return out;
}

function viewSimulate(step, ev) {
  const v = ev.verdict;
  if (!v) return [];
  const sm = ev.summary || {};
  const verdict = ["Verdict", statusPill({ pass: "succeeded", degraded: "warn", fail: "failed" }[v.status] || "notrun", v.status)];
  return [kv([["pulls failed", sm.pulls_failed], ["requests failed", sm.requests_failed],
      ["tokens minted", sm.tokens_minted], ["first failure (s)", sm.first_failure_s]], verdict),
    v.root_cause ? h("p", { class: "small", text: typeof v.root_cause === "string" ? v.root_cause : JSON.stringify(v.root_cause) }) : null];
}

// One plain sentence, e.g. "Imports 1 existing resource and creates 1. Nothing is changed or deleted."
function summaryChips(s) {
  const n = (k) => s[k] || 0;
  const parts = [];
  if (n("import")) parts.push(`imports ${n("import")} existing resource${n("import") > 1 ? "s" : ""}`);
  if (n("create")) parts.push(`creates ${n("create")}`);
  if (n("update")) parts.push(`updates ${n("update")}`);
  if (n("replace")) parts.push(`replaces ${n("replace")}`);
  if (n("delete")) parts.push(`deletes ${n("delete")}`);
  const head = parts.length ? parts.join(", ").replace(/, ([^,]*)$/, " and $1") : "changes nothing";
  const tail = !n("update") && !n("replace") && !n("delete") && parts.length ? " Nothing is changed or deleted." : "";
  return h("p", { class: `plan-sentence${n("replace") || n("delete") ? " danger-text" : ""}`,
    text: `This plan ${head}.${tail}` });
}

function viewPlan(step, ev) {
  const p = ev.plan, w = ev.workspace;
  if (!p) return [];
  return [
    kv([["workspace", w?.workspace_key], ["directory", w?.dir], ["targets in workspace", (w?.targets || []).join(", ")],
      ["plan sha256", p.sha256]]),
    ...(w?.warnings || []).map((x) => h("p", { class: "small warn-text", text: x })),
    summaryChips(p.summary),
    p.changes.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "Action" }), h("th", { text: "Resource" }))),
      h("tbody", {}, p.changes.map((c) => h("tr", { class: `act-${c.action}` },
        h("td", {}, h("span", { class: `act-pill act-${c.action}`, text: c.action })), h("td", { class: "mono", text: c.address }))))))
      : h("p", { class: "muted", text: "No changes: the workspace already matches." }),
  ];
}

async function approve(step, body, btn, errBox) {
  btn.disabled = true;
  fill(errBox);
  try {
    await api(`/api/runs/${rs.id}/approve`, { method: "POST", body: { step, ...body } });
    rs.userPicked = false;
    scheduleRefetch();
  } catch (e) {
    fill(errBox, h("p", { class: "error-box", role: "alert", text: e.message }));
    btn.disabled = false;
  }
}

function viewApply(step, ev) {
  const out = [];
  const a = step.approval;
  if (step.status === "needs_approval" && a) {
    const errBox = h("div");
    const btn = h("button", { type: "button", class: "primary", text: "Approve and apply this plan" });
    out.push(h("div", { class: "gate" },
      h("p", {}, "Terraform applies exactly the plan shown in ", h("b", { text: "Plan" }), " (sha256 ",
        h("code", { text: a.plan_sha256.slice(0, 16) + "…" }), ") and nothing else."),
      summaryChips(a.summary)));
    let input = null;
    if (a.destructive?.length) {
      input = h("input", { type: "text", spellcheck: "false", autocomplete: "off", "aria-label": "Type the workspace name to confirm" });
      btn.disabled = true;
      input.addEventListener("input", () => { btn.disabled = input.value !== a.confirmation_phrase; });
      out.push(h("div", { class: "danger-box" },
        h("b", { text: `This plan deletes or replaces ${a.destructive.length} resource(s):` }),
        h("ul", {}, a.destructive.map((d) => h("li", { class: "mono", text: d }))),
        h("label", { class: "field" }, h("span", {}, "Type ", h("code", { text: a.confirmation_phrase }), " to confirm"), input)));
    }
    btn.addEventListener("click", () => approve("apply",
      { plan_sha256: a.plan_sha256, ...(input ? { typed_confirmation: input.value } : {}) }, btn, errBox));
    out.push(btn, errBox);
  }
  if (ev.result) out.push(outcome(ev.result.applied ? "Applied." : `Not applied: ${ev.result.reason || "see the run log"}.`,
    ev.result.applied));
  if (ev.outputs) out.push(h("h3", { text: "Workspace outputs" }), kv(Object.entries(ev.outputs)));
  return out;
}

function viewWire(step, ev) {
  const out = [];
  const d = ev.diff;
  if (d) {
    out.push(h("p", { class: "small muted", text: `Target ${d.target}` }));
    out.push(h("div", { class: "table-wrap" }, h("table", { class: "diff" },
      h("thead", {}, h("tr", {}, ["", "Setting", "Current", "Desired"].map((t) => h("th", { text: t })))),
      h("tbody", {}, d.diff.map((r) => h("tr", { class: `diff-${r.action}` },
        h("td", { class: "diff-act", text: { same: "=", add: "+", change: "~" }[r.action] || r.action }),
        h("td", { class: "mono", text: `${r.scope}: ${r.setting}` }),
        h("td", { class: "mono", text: r.current ?? "(not set)" }),
        h("td", { class: "mono", text: r.desired ?? "" })))))));
    if (d.display_commands?.length) out.push(h("h3", { text: "Commands" }), h("pre", { class: "cmds", text: d.display_commands.join("\n") }));
    for (const n of d.notes || []) out.push(h("p", { class: "small", text: n }));
    if (d.iac) out.push(h("details", {}, h("summary", { text: "Settings for your own IaC" }), h("pre", { class: "cmds", text: JSON.stringify(d.iac, null, 2) })));
  }
  const a = step.approval;
  if (step.status === "needs_approval" && a) {
    const errBox = h("div");
    const btn = h("button", { type: "button", class: "primary", text: "Approve and apply these settings" });
    btn.addEventListener("click", () => approve("wire", { diff_sha256: a.diff_sha256 }, btn, errBox));
    out.unshift(h("div", { class: "gate" }, h("p", { text: "These commands change the target's settings. If the target changes before they run, you'll be shown the new diff." }), btn, errBox));
  }
  if (ev.result) {
    const r = ev.result;
    const text = r.applied ? "Settings applied." : r.reason === "already wired"
      ? "Nothing to change: the app already has these settings." : `Not applied: ${r.reason || "see the run log"}.`;
    out.push(outcome(r.verified ? `${text} Checked against the live app.` : text, r.applied || r.verified));
  }
  return out;
}

// ----------------------------------------------------------------- log

// A log line reads "<event> → <result> (<seconds>)": each part gets its own column.
const LOG_LINE = /^(.*?) → (.+?)(?: \((\d+(?:\.\d+)?s)\))?$/;
const BAD = /^(exit [1-9]|[45]\d\d\b)|fail|error|denied|forbidden/i;
const STEP_TONE = { succeeded: "ok", failed: "bad", cancelled: "bad", needs_approval: "info", running: "info", skipped: "info" };

// "GET https://…": the method becomes a tag so the URLs line up.
function logEvent(event) {
  const m = /^(GET|POST|PUT|PATCH|DELETE|HEAD) (\S.*)$/.exec(event);
  return m ? [h("div", { class: "log-call" }, h("span", { class: `log-verb verb-${m[1].toLowerCase()}`, text: m[1] }),
    h("span", { text: m[2] }))] : [event];
}

// One row's parts, for the table and for Copy.
function logParts(e) {
  if (e.kind === "step") {
    const title = rs.snap?.steps?.find((s) => s.name === e.name)?.title || e.name;
    const word = e.status === "running" ? "started" : e.status.replaceAll("_", " ");
    return { step: true, event: `${title} ${word}`, result: word, tone: STEP_TONE[e.status] || "info", took: stepTook(e),
      error: e.error, note: e.status !== "running" ? e.note : null };
  }
  const m = LOG_LINE.exec(e.line || "");
  const [event, result, took] = m ? [m[1], m[2], m[3] || ""] : [e.line || "", "", ""];
  const tone = result ? (/^(exit 0|[23]\d\d)\b/.test(result) ? "ok" : BAD.test(result) ? "bad" : "info") : BAD.test(event) ? "bad" : "";
  return { step: false, event, result, tone, took };
}

function logRow(e, n) {
  const p = logParts(e);
  return h("tr", { class: [p.step ? "log-step-row" : "", p.tone === "bad" ? "log-row-bad" : ""].join(" ").trim() || null },
    h("td", { class: "log-n", text: String(n) }),
    h("td", { class: "log-t", text: (e.t || "").slice(11, 19) }),
    h("td", { class: "log-msg" }, p.step ? h("b", { text: p.event }) : logEvent(p.event),
      p.error ? h("div", { class: "log-err", text: p.error }) : null, p.note ? h("div", { class: "log-note", text: p.note }) : null),
    h("td", { class: "log-res-cell" }, p.result ? h("span", { class: `log-res log-${p.tone || "info"}`, text: p.result }) : null),
    h("td", { class: "log-took", text: p.took || "" }));
}

const stepOf = (e) => (e.kind === "step" ? e.name : e.step);

// A step's end row shows how long it ran, from its "started" row.
function stepTook(e) {
  if (e.kind !== "step" || e.status === "running") return "";
  const start = rs.log.find((x) => x.kind === "step" && x.name === e.name && x.status === "running");
  const ms = start ? Date.parse(e.t) - Date.parse(start.t) : NaN;
  return Number.isFinite(ms) && ms >= 0 ? `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s` : "";
}

// The selected step's lines. Command lines ("$ az …") stay in the server's log.
function stepLines() {
  return rs.log.filter((e) => (e.kind === "step" ? e.name && e.status : !/^\$ /.test(e.line || "")) && stepOf(e) === rs.selected);
}

function renderLog() {
  const lines = stepLines();
  $("run-log-count").textContent = lines.length ? String(lines.length) : "";
  $("run-log-copy").disabled = !lines.length;
  fill($("run-log"), lines.length
    ? h("table", { class: "log-table" },
      h("thead", {}, h("tr", {}, ["#", "Time", "Event", "Result", "Took"].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", {}, lines.map((e, i) => logRow(e, i + 1))))
    : h("p", { class: "log-empty", text: "No log lines for this step." }));
}

async function copyLog() {
  const text = stepLines().map((e, i) => {
    const p = logParts(e);
    return [i + 1, (e.t || "").slice(11, 19), p.event, p.result, p.took, p.error || ""].filter((x) => x !== "").join("\t");
  }).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    note("Log copied", "ok");
  } catch {
    note("Copy is blocked in this browser", "error");
  }
}

// ----------------------------------------------------------------- stream

async function openRun(id) {
  if (rs.abort) rs.abort.abort();
  Object.assign(rs, { id, snap: null, selected: null, log: [], userPicked: false });
  try {
    rs.snap = await api(`/api/runs/${encodeURIComponent(id)}`);
  } catch (e) {
    fill($("run-detail"), h("p", { class: "error-box", text: e.status === 404 ? "This run no longer exists." : e.message }));
    $("run-title").textContent = `Run ${id}`;
    return;
  }
  render();
  renderLog();
  // Live runs stream as they go; a finished run's stream replays its log, then ends.
  const ctl = new AbortController();
  rs.abort = ctl;
  streamEvents(`/api/runs/${encodeURIComponent(id)}/events`, (kind, data) => {
    if (rs.id !== id) return;
    if (kind === "snapshot" || kind === "done") {
      rs.snap = data;
      render();
      return;
    }
    if (data.kind === "log" || data.kind === "step") {
      // Step changes are log rows too: every step shows when it started and how it ended.
      rs.log.push(data);
      if (rs.log.length > 4000) rs.log.splice(0, rs.log.length - 4000);
      if (stepOf(data) === rs.selected) renderLog();
    }
    if (data.kind !== "log") scheduleRefetch();
  }, ctl.signal).catch((e) => { if (e.name !== "AbortError") note(`Event stream ended: ${e.message}`, "warn"); });
}

// Step changes arrive as small events; fetch the full snapshot (with evidence) at most every 150 ms.
function scheduleRefetch() {
  if (rs.refetch) return;
  rs.refetch = setTimeout(async () => {
    rs.refetch = null;
    const id = rs.id;
    try {
      const snap = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (rs.id === id) { rs.snap = snap; render(); }
    } catch { /* the stream will report problems */ }
  }, 150);
}

// ----------------------------------------------------------------- rendering

function pickStep(snap) {
  if (rs.userPicked && rs.selected) return rs.selected;
  const waiting = snap.steps.find((s) => s.status === "needs_approval");
  if (waiting) return waiting.name;
  const failed = snap.steps.find((s) => s.status === "failed");
  if (failed) return failed.name;
  if (snap.current) return snap.current;
  const done = snap.steps.filter((s) => s.status === "succeeded");
  return done.length ? done[done.length - 1].name : shownSteps(snap)[0]?.name;
}

// Steps that aren't built yet (planned) are left out rather than shown as skipped.
const shownSteps = (snap) => snap.steps.filter((s) => !s.planned);

function renderHead() {
  const snap = rs.snap;
  const live = LIVE.has(snap.status);
  $("run-title").textContent = `Run ${snap.id}`;
  fill($("run-pill"), statusPill(snap.status));
  const took = dur(runDuration(snap));
  fill($("run-facts"),
    h("span", { title: stamp(snap.created), text: `Started ${ago(snap.created)}` }),
    took ? h("span", { text: took }) : null,
    h("a", { href: href.workload(rs.name, "runs"), text: `Back to ${rs.name}` }));
  $("run-cancel").hidden = !live;
  $("run-delete").hidden = live;
  // A live run's clock and step durations tick.
  if (live && !rs.tick) rs.tick = setInterval(() => rs.snap && (renderHead(), renderTree()), 1000);
  if (!live && rs.tick) { clearInterval(rs.tick); rs.tick = null; }
}

function renderTree() {
  const steps = shownSteps(rs.snap);
  const focused = document.activeElement?.closest?.("#run-steps") ? document.activeElement.dataset.step : null;
  fill($("run-steps"), steps.map((s) => {
    const took = s.started ? dur(stepDuration(s)) : "";
    const btn = h("button", {
      type: "button", class: `step${s.name === rs.selected ? " selected" : ""}`, "data-step": s.name,
      "aria-current": s.name === rs.selected ? "step" : null, tabindex: s.name === rs.selected ? "0" : "-1",
      onclick: () => select(s.name),
    }, statusIcon(s.status), h("span", { class: "step-title", text: s.title }), h("span", { class: "step-took", text: took }));
    return h("li", {}, btn);
  }));
  if (focused) $("run-steps").querySelector(`[data-step="${focused}"]`)?.focus();
}

function select(name, focus = false) {
  rs.selected = name;
  rs.userPicked = true;
  renderTree();
  renderDetail(rs.snap.steps.find((s) => s.name === name));
  renderLog();
  if (focus) $("run-steps").querySelector(`[data-step="${name}"]`)?.focus();
}

function render() {
  const snap = rs.snap;
  if (!snap) return;
  const before = rs.selected;
  rs.selected = pickStep(snap);
  renderHead();
  renderTree();
  renderDetail(snap.steps.find((s) => s.name === rs.selected));
  if (before !== rs.selected) renderLog();
}

function renderDetail(step) {
  if (!step) return fill($("run-detail"));
  const ev = step.evidence || {};
  const took = step.started ? dur(stepDuration(step)) : "";
  const parts = [h("div", { class: "detail-head" }, h("h2", { text: step.title }), statusPill(step.status),
    took ? h("span", { class: "muted small", text: took }) : null)];
  if (step.error) parts.push(h("p", { class: "error-box", text: step.error }));
  if (step.note) parts.push(h("p", { class: "muted small", text: step.note }));
  const view = { preflight: viewPreflight, discover: viewDiscover, simulate: viewSimulate, publish: viewPublish,
    plan: viewPlan, apply: viewApply, wire: viewWire }[step.name];
  if (view) parts.push(...view(step, ev));
  if (parts.length === 1) parts.push(h("p", { class: "muted", text: step.status === "pending" ? "Not started yet." : "No evidence." }));
  fill($("run-detail"), parts);
}

// ----------------------------------------------------------------- actions

async function cancel(ev) {
  if (!armed(ev.currentTarget, "Click again to cancel")) return;
  try {
    await api(`/api/runs/${encodeURIComponent(rs.id)}/cancel`, { method: "POST", body: {} });
    note("Cancelling…", "busy");
  } catch (e) {
    note(e.message, "error");
  }
}

async function removeRun(ev) {
  if (!armed(ev.currentTarget, "Click again to delete")) return;
  try {
    await api(`/api/runs/${encodeURIComponent(rs.id)}`, { method: "DELETE" });
  } catch (e) {
    return note(e.message, "error");
  }
  location.assign(href.workload(rs.name, "runs"));
}

// Up and down move through the steps; Home and End jump to the ends.
function onTreeKey(e) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key) || !rs.snap) return;
  e.preventDefault();
  const names = shownSteps(rs.snap).map((s) => s.name);
  const i = names.indexOf(rs.selected);
  const j = e.key === "Home" ? 0 : e.key === "End" ? names.length - 1
    : Math.min(names.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)));
  if (names[j] && names[j] !== rs.selected) select(names[j], true);
}

export async function mountRun(page, { name, id }) {
  rs.name = name;
  rs.id = id;
  page.replaceChildren(template("tpl-run"));
  document.title = `Run ${id} · ${name}`;
  $("run-cancel").addEventListener("click", cancel);
  $("run-delete").addEventListener("click", removeRun);
  $("run-log-copy").addEventListener("click", copyLog);
  $("run-steps").addEventListener("keydown", onTreeKey);
  await openRun(id);
}


