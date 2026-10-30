// Runs tab: the 9-step pipeline for one config, with the approval gates inline.
// Rendered with DOM APIs only (no innerHTML). Everything shown has been
// redacted by the server; approvals send back only the hashes it showed.

const $ = (id) => document.getElementById(id);
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

let api, h, streamEvents, selectTab, ofCloud;

const KIND_LABELS = {
  gcp_run_service: "Cloud Run service", gcp_run_job: "Cloud Run job", gcp_run_function: "Cloud Run function",
  azure_container_app: "Container App", azure_functions_aca: "Functions on Container Apps",
  azure_functions_premium: "Functions Premium/Dedicated", azure_functions_flex: "Functions Flex Consumption",
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
}

export async function startRun(cfg) {
  selectTab("tab-runs");
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
  if (!LIVE.has(rs.snap.status)) return;
  const ctl = new AbortController();
  rs.abort = ctl;
  streamEvents(`/api/runs/${id}/events`, (kind, data) => {
    if (rs.id !== id) return;
    if (kind === "snapshot" || kind === "done") {
      rs.snap = data;
      render();
      if (kind === "done") refreshList();
      return;
    }
    if (data.kind === "log") {
      rs.log.push(data);
      if (rs.log.length > 2000) rs.log.splice(0, rs.log.length - 2000);
      renderLog();
    } else {
      scheduleRefetch();
    }
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
  fill($("run-head"),
    h("div", {}, h("b", { text: snap.name }), " ", h("span", { class: "tag", text: KIND_LABELS[snap.kind] || snap.kind }), " ",
      h("span", { class: `pill run-${snap.status}`, text: snap.status.replaceAll("_", " ") })),
    h("div", { class: "small muted", text: `run ${snap.id} · started ${snap.created}` }),
    LIVE.has(snap.status) ? h("button", { type: "button", class: "secondary danger", onclick: cancel, text: "Cancel run" }) : null);
  note(snap.status === "waiting_approval" ? "Waiting for your approval." : LIVE.has(snap.status) ? "Running…" : "", LIVE.has(snap.status) ? "busy" : "");

  // Steps that aren't built yet (planned) are left out rather than shown as skipped.
  fill($("run-steps"), snap.steps.filter((s) => !s.planned).map((s, i) => h("li", {},
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
  const parts = [h("h2", { text: step.title })];
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
  return h("p", { class: `outcome${ok ? "" : " is-warn"}` }, h("b", { text: ok ? "✓" : "!" }), text);
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
    h("span", { class: "chk-icon", text: { ok: "✓", warn: "!", fail: "✗", skip: "–" }[c.status] || "?" }),
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
        h("td", { text: c.action }), h("td", { class: "mono", text: c.address }))))))
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

function renderLog() {
  const pre = $("run-log");
  // Steps and outcomes only: command lines ("$ az …") stay in the server's log.
  pre.textContent = rs.log.filter((e) => !/^\$ /.test(e.line || "")).map((e) => `${(e.t || "").slice(11, 19)} ${e.step ? `[${e.step}] ` : ""}${e.line}`).join("\n");
  pre.scrollTop = pre.scrollHeight;
}

async function cancel(ev) {
  const btn = ev.currentTarget;
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.textContent = "Click again to cancel";
    setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "Cancel run"; }, 4000);
    return;
  }
  try {
    await api(`/api/runs/${rs.id}/cancel`, { method: "POST", body: {} });
  } catch (e) {
    note(e.message, "error");
  }
}

// ----------------------------------------------------------------- init

export async function initRuns(deps) {
  ({ api, h, streamEvents, selectTab, ofCloud } = deps);
  $("runs-list").addEventListener("change", (e) => openRun(e.target.value));
  $("runs-refresh").addEventListener("click", refreshList);
  await refreshList();
  if (rs.runs.length) await openRun(rs.runs[0].id);
  return { startRun };
}
