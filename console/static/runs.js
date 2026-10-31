// Pipeline runs: the 9-step pipeline for one config, with the approval gates inline.
// Rendered with DOM APIs only (no innerHTML). Everything shown has been
// redacted by the server; approvals send back only the hashes it showed.

const $ = (id) => document.getElementById(id);
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

let api, h, streamEvents, showSection, ofCloud;

const KIND_LABELS = {
  gcp_run_service: "Cloud Run service", gcp_run_job: "Cloud Run job", gcp_run_function: "Cloud Run function",
  azure_container_app: "Container App", azure_functions_aca: "Functions on Container Apps",
  azure_functions_premium: "Functions Premium/Dedicated", azure_functions_flex: "Functions Flex Consumption",
  aws_lambda: "Lambda function", aws_ecs_task: "ECS task", aws_app_runner: "App Runner service",
  openshift_knative_service: "Knative service",
};
const rs = { runs: [], id: null, snap: null, selected: null, abort: null, log: [], refetch: null, userPicked: false };

const ICON = { pending: "○", running: "◐", succeeded: "✓", failed: "✗", needs_approval: "!", skipped: "–", cancelled: "⊘" };
const LIVE = new Set(["running", "waiting_approval"]);

// ----------------------------------------------------------------- list and stream

async function refreshList() {
  try {
    rs.runs = (await api("/api/runs")).runs.filter((r) => ofCloud(r.kind));
  } catch (e) {
    return note(`Could not list runs: ${e.message}`, "error");
  }
  const sel = $("runs-list");
  fill(sel, rs.runs.length ? rs.runs.map((r) => h("option", { value: r.id, text: `${r.name} · ${r.status.replaceAll("_", " ")} · ${r.created.slice(11, 19)}Z` }))
    : h("option", { value: "", text: "no runs yet" }));
  if (rs.id) sel.value = rs.id;
  // No runs: one empty state instead of empty Steps and Log cards.
  document.querySelector(".runs-layout").hidden = !rs.runs.length;
  $("runs-count").textContent = rs.runs.length || "";
  $("runs-delete").hidden = $("runs-clear").hidden = !rs.runs.length;
  if (!rs.runs.length) fill($("run-head"), h("p", { class: "muted", text: "No runs yet. Run pipeline above starts one." }));
}

export async function startRun(cfg) {
  showSection("sec-runs");
  note("Starting the pipeline…", "busy");
  let res;
  try {
    res = await api("/api/runs", { method: "POST", body: { config: cfg } });
  } catch (e) {
    return note(e.message, "error");
  }
  await refreshList();
  await openRun(res.run_id);
}

async function openRun(id) {
  if (!id) return;
  if (rs.abort) rs.abort.abort();
  Object.assign(rs, { id, snap: null, selected: null, log: [], userPicked: false });
  $("runs-list").value = id;
  renderLog();
  try {
    rs.snap = await api(`/api/runs/${id}`);
  } catch (e) {
    return note(e.message, "error");
  }
  render();
  // Live runs stream as they go; a finished run's stream replays its log, then ends.
  const wasLive = LIVE.has(rs.snap.status);
  const ctl = new AbortController();
  rs.abort = ctl;
  streamEvents(`/api/runs/${id}/events`, (kind, data) => {
    if (rs.id !== id) return;
    if (kind === "snapshot" || kind === "done") {
      rs.snap = data;
      render();
      if (kind === "done" && wasLive) refreshList();
      return;
    }
    if (data.kind === "log" || data.kind === "step") {
      // Step changes are log rows too: every step shows when it started and how it ended.
      rs.log.push(data);
      if (rs.log.length > 2000) rs.log.splice(0, rs.log.length - 2000);
      renderLog();
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
      const snap = await api(`/api/runs/${id}`);
      if (rs.id === id) { rs.snap = snap; render(); }
    } catch { /* the stream will report problems */ }
  }, 150);
}

// ----------------------------------------------------------------- rendering

function note(text, kind = "") {
  const el = $("runs-status");
  el.textContent = text || "";
  el.className = `small status-${kind}`;
}

function pickStep(snap) {
  if (rs.userPicked && rs.selected) return rs.selected;
  const waiting = snap.steps.find((s) => s.status === "needs_approval");
  if (waiting) return waiting.name;
  const failed = snap.steps.find((s) => s.status === "failed");
  if (failed) return failed.name;
  if (snap.current) return snap.current;
  const done = snap.steps.filter((s) => s.status === "succeeded");
  return done.length ? done[done.length - 1].name : snap.steps[0].name;
}

function render() {
  const snap = rs.snap;
  if (!snap) return;
  const listed = rs.runs.find((r) => r.id === snap.id);
  if (listed && listed.status !== snap.status) refreshList();
  rs.selected = pickStep(snap);
  $("runs-delete").disabled = LIVE.has(snap.status);
  const shown = snap.steps.filter((s) => !s.planned);
  const done = shown.filter((s) => s.status === "succeeded").length;
  fill($("run-head"),
    h("div", { class: "run-title" },
      h("h2", { text: snap.name }),
      h("span", { class: `pill run-${snap.status}`, text: snap.status.replaceAll("_", " ") }),
      LIVE.has(snap.status) ? h("button", { type: "button", class: "secondary danger small-btn", onclick: cancel, text: "Cancel run" }) : null),
    // Each fact is a tile: label above value.
    h("dl", { class: "essentials" },
      h("div", {}, h("dt", { text: "Run ID" }), h("dd", { class: "mono", text: snap.id })),
      h("div", {}, h("dt", { text: "Workload type" }), h("dd", { text: KIND_LABELS[snap.kind] || snap.kind })),
      h("div", {}, h("dt", { text: "Started" }), h("dd", { text: (snap.created || "").replace("T", " ").replace("Z", " UTC") })),
      h("div", {}, h("dt", { text: "Steps" }), h("dd", {}, h("span", { class: "meter", role: "img", "aria-label": `${done} of ${shown.length} steps succeeded` },
        shown.map((s) => h("i", { class: `meter-seg st-${s.status}` }))), ` ${done} / ${shown.length}`))));
  note("", "");  // the run's status is the pill on its card

  // Steps that aren't built yet (planned) are left out rather than shown as skipped.
  fill($("run-steps"), shown.map((s, i) => h("li", {},
    h("button", {
      type: "button", class: `step st-${s.status}${s.name === rs.selected ? " selected" : ""}`,
      "aria-current": s.name === rs.selected ? "step" : null,
      onclick: () => { rs.selected = s.name; rs.userPicked = true; render(); },
    }, h("span", { class: "st-icon", "aria-hidden": "true", text: ICON[s.status] || "?" }),
      h("span", { class: "st-name", text: `${i + 1}. ${s.title}` }),
      h("span", { class: "st-status", text: s.status.replaceAll("_", " ") })))));

  renderDetail(snap.steps.find((s) => s.name === rs.selected));
}

function renderDetail(step) {
  const ev = step.evidence || {};
  const parts = [h("div", { class: "detail-head" }, h("h2", { text: step.title }),
    h("span", { class: `pill step-${step.status}`, text: step.status.replaceAll("_", " ") }))];
  if (step.error) parts.push(h("p", { class: "error-box", text: step.error }));
  if (step.note) parts.push(h("p", { class: "muted small", text: step.note }));
  const view = { preflight: viewPreflight, discover: viewDiscover, simulate: viewSimulate, publish: viewPublish,
    plan: viewPlan, apply: viewApply, wire: viewWire }[step.name];
  if (view) parts.push(...view(step, ev));
  if (parts.length === 1) parts.push(h("p", { class: "muted", text: step.status === "pending" ? "Not started yet." : "No evidence." }));
  fill($("run-detail"), parts);
}

// ----------------------------------------------------------------- per-step views

function outcome(text, ok) {
  return h("p", { class: `outcome${ok ? "" : " is-warn"}` },
    h("span", { class: `st-dot st-dot-${ok ? "ok" : "warn"}`, "aria-hidden": "true", text: ok ? "✓" : "!" }), text);
}

function kv(rows) {
  return h("table", { class: "facts" }, h("tbody", {}, rows.filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => h("tr", {}, h("th", { scope: "row", text: k }),
      h("td", { class: "mono", text: typeof v === "object" ? JSON.stringify(v) : String(v) })))));
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
  return [h("ul", { class: "checklist" }, r.checks.map((c) => h("li", { class: `chk chk-${c.status}` },
    h("span", { class: `chk-icon st-dot st-dot-${c.status}`, "aria-label": c.status, text: { ok: "✓", warn: "!", fail: "✕", skip: "–" }[c.status] || "?" }),
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
  return [h("p", {}, "Verdict ", h("span", { class: `pill v-${v.status}`, text: v.status })),
    v.root_cause ? h("p", { class: "small", text: typeof v.root_cause === "string" ? v.root_cause : JSON.stringify(v.root_cause) }) : null,
    ev.summary ? kv([["pulls failed", ev.summary.pulls_failed], ["requests failed", ev.summary.requests_failed],
      ["tokens minted", ev.summary.tokens_minted], ["first failure (s)", ev.summary.first_failure_s]]) : null];
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

// A log line reads "<event> → <result> (<seconds>)": each part gets its own column.
const LOG_LINE = /^(.*?) → (.+?)(?: \((\d+(?:\.\d+)?s)\))?$/;
const BAD = /^(exit [1-9]|[45]\d\d\b)|fail|error|denied|forbidden/i;

// "GET https://…": the method becomes a tag so the URLs line up.
function logEvent(event) {
  const m = /^(GET|POST|PUT|PATCH|DELETE|HEAD) (\S.*)$/.exec(event);
  return m ? [h("div", { class: "log-call" }, h("span", { class: `log-verb verb-${m[1].toLowerCase()}`, text: m[1] }),
    h("span", { text: m[2] }))] : [event];
}

const STEP_TONE = { succeeded: "ok", failed: "bad", cancelled: "bad", needs_approval: "info", running: "info", skipped: "info" };

function stepRow(e, showStep, took) {
  const title = rs.snap?.steps?.find((s) => s.name === e.name)?.title || e.name;
  const status = e.status === "running" ? "started" : e.status.replaceAll("_", " ");
  const tone = STEP_TONE[e.status] || "info";
  return h("tr", { class: `log-step-row${tone === "bad" ? " log-row-bad" : ""}` },
    h("td", { class: "log-t", text: (e.t || "").slice(11, 19) }),
    h("td", { class: "log-step-cell" }, showStep ? h("span", { class: "log-step", text: title }) : null),
    h("td", { class: "log-msg" }, h("b", { text: `${title} ${status}` }), e.error ? h("div", { class: "log-err", text: e.error }) : null,
      e.note && e.status !== "running" ? h("div", { class: "log-note", text: e.note }) : null),
    h("td", { class: "log-res-cell" }, h("span", { class: `log-res log-${tone}`, text: status })),
    h("td", { class: "log-took", text: took || "" }));
}

function logRow(e, showStep, stepTime) {
  if (e.kind === "step") return stepRow(e, showStep, stepTime);
  const m = LOG_LINE.exec(e.line || "");
  const [event, result, took] = m ? [m[1], m[2], m[3] || ""] : [e.line || "", "", ""];
  const tone = result ? (/^(exit 0|[23]\d\d)\b/.test(result) ? "ok" : BAD.test(result) ? "bad" : "info")
    : BAD.test(event) ? "bad" : "";
  return h("tr", { class: tone === "bad" ? "log-row-bad" : null },
    h("td", { class: "log-t", text: (e.t || "").slice(11, 19) }),
    h("td", { class: "log-step-cell" }, showStep && e.step ? h("span", { class: "log-step", text: e.step.replaceAll("_", " ") }) : null),
    h("td", { class: "log-msg" }, ...logEvent(event)),
    h("td", { class: "log-res-cell" }, result ? h("span", { class: `log-res log-${tone}`, text: result }) : null),
    h("td", { class: "log-took", text: took }));
}

const stepOf = (e) => (e.kind === "step" ? e.name : e.step);

// A step's end row shows how long it ran, from its "started" row.
function stepTook(e) {
  if (e.kind !== "step" || e.status === "running") return "";
  const start = rs.log.find((x) => x.kind === "step" && x.name === e.name && x.status === "running");
  const ms = start ? Date.parse(e.t) - Date.parse(start.t) : NaN;
  return Number.isFinite(ms) && ms >= 0 ? `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s` : "";
}

function renderLog() {
  // Steps and outcomes only: command lines ("$ az …") stay in the server's log.
  const lines = rs.log.filter((e) => (e.kind === "step" ? e.name && e.status : !/^\$ /.test(e.line || "")));
  $("run-log-count").textContent = lines.length ? String(lines.length) : "";
  // The step shows once, where it starts: the rows below it belong to it.
  fill($("run-log"), lines.length
    ? h("table", { class: "log-table" },
      h("thead", {}, h("tr", {}, ["Time", "Step", "Event", "Result", "Took"].map((t) => h("th", { scope: "col", text: t })))),
      h("tbody", {}, lines.map((e, i) => logRow(e, i === 0 || stepOf(lines[i - 1]) !== stepOf(e), stepTook(e)))))
    : h("p", { class: "log-empty", text: "No log lines yet." }));
}

// Two clicks for anything destructive: the first arms the button for four seconds.
function armed(btn, prompt) {
  if (btn.dataset.armed === "1") {
    btn.dataset.armed = "";
    btn.textContent = btn.dataset.label;
    return true;
  }
  btn.dataset.label ||= btn.textContent;
  btn.dataset.armed = "1";
  btn.textContent = prompt;
  setTimeout(() => { btn.dataset.armed = ""; btn.textContent = btn.dataset.label; }, 4000);
  return false;
}

async function cancel(ev) {
  if (!armed(ev.currentTarget, "Click again to cancel")) return;
  try {
    await api(`/api/runs/${rs.id}/cancel`, { method: "POST", body: {} });
  } catch (e) {
    note(e.message, "error");
  }
}

// Delete the open run, or every finished run of this cloud. Active runs are never removed.
async function removeRun(ev) {
  if (!rs.id || !armed(ev.currentTarget, "Click again to delete")) return;
  try {
    await api(`/api/runs/${rs.id}`, { method: "DELETE" });
  } catch (e) {
    return note(e.message, "error");
  }
  await afterCleanup("Run deleted.");
}

async function clearRuns(ev) {
  if (!armed(ev.currentTarget, "Click again to clear")) return;
  let res;
  try {
    res = await api(`/api/runs?cloud=${encodeURIComponent(rs.cloud)}`, { method: "DELETE" });
  } catch (e) {
    return note(e.message, "error");
  }
  await afterCleanup(`${res.runs_deleted} run${res.runs_deleted === 1 ? "" : "s"} cleared`
    + (res.runs_kept ? `, ${res.runs_kept} still active` : "") + ".");
}

async function afterCleanup(message) {
  if (rs.abort) rs.abort.abort();
  Object.assign(rs, { id: null, snap: null, log: [] });
  await refreshList();
  if (rs.runs.length) await openRun(rs.runs[0].id);
  note(message, "ok");
}

// ----------------------------------------------------------------- init

export async function initRuns(deps) {
  ({ api, h, streamEvents, showSection, ofCloud } = deps);
  rs.cloud = deps.cloud;
  $("runs-list").addEventListener("change", (e) => openRun(e.target.value));
  $("runs-delete").addEventListener("click", removeRun);
  $("runs-clear").addEventListener("click", clearRuns);
  $("runs-refresh").addEventListener("click", refreshList);
  await refreshList();
  if (rs.runs.length) await openRun(rs.runs[0].id);
  return { startRun };
}
