// Shared UI pieces: a renderer for runbook data, tables, the run control
// (prepare → approve → queue → live events) and the event-stream reader.

import { api, h, fill, rich, state } from "./app.js";

export const $ = (id) => document.getElementById(id);

const HUMAN = (k) => String(k).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

// Render knowledge data generically, so no runbook fact is dropped: strings become
// paragraphs, string lists become bullets, uniform object lists become tables.
export function doc(value, depth = 0) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return h("p", {}, rich(value));
  if (typeof value === "number" || typeof value === "boolean") return h("p", {}, String(value));
  if (Array.isArray(value)) {
    if (!value.length) return null;
    if (value.every((v) => typeof v === "string")) return h("ul", {}, value.map((v) => h("li", {}, rich(v))));
    if (value.every((v) => v && typeof v === "object" && !Array.isArray(v))) {
      const keys = [...new Set(value.flatMap((v) => Object.keys(v)))];
      const simple = keys.every((k) => value.every((v) => v[k] === undefined || ["string", "number", "boolean"].includes(typeof v[k])));
      if (simple) return table(keys, value.map((v) => keys.map((k) => v[k])));
      return h("div", {}, value.map((v) => h("div", { class: "card doc-card" }, doc(v, depth + 1))));
    }
    return h("ul", {}, value.map((v) => h("li", {}, doc(v, depth + 1))));
  }
  return h("div", { class: "doc" }, Object.entries(value).map(([k, v]) => {
    if (k === "id" || k === "n") return null;
    if (typeof v === "string" && v.length < 90 && depth > 0) return h("p", {}, h("strong", {}, `${HUMAN(k)}: `), rich(v));
    return h("div", {}, h(depth ? "h4" : "h3", {}, HUMAN(k)), doc(v, depth + 1));
  }));
}

export function table(cols, rows, cls = "grid small") {
  const cell = (v) => (v instanceof Node || Array.isArray(v) ? v : typeof v === "boolean" ? (v ? "yes" : "no") : rich(v ?? ""));
  return h("table", { class: cls }, h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, HUMAN(c))))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v) => h("td", {}, cell(v)))))));
}

export const pill = (status) => h("span", { class: `pill ${String(status).toLowerCase().replace(/[^a-z]/g, "")}` }, String(status));

export function section(title, ...kids) {
  return h("section", { class: "block" }, h("h2", {}, title), ...kids);
}

export function needConfig(el) {
  if (state.config) return false;
  fill(el, h("p", { class: "callout warn" }, "No saved configuration yet. Create one on the Configure tab."));
  return true;
}

// Server-sent events over fetch, so the token stays in a header.
export async function stream(path, onFrame) {
  const resp = await fetch(path, { headers: { "X-Console-Token": state.token }, credentials: "omit", cache: "no-store" });
  if (!resp.ok || !resp.body) throw new Error(`event stream failed: HTTP ${resp.status}`);
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let kind = "message", data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) kind = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onFrame(kind, JSON.parse(data));
    }
  }
}

// The run control. dryRun: show a DryRun switch (on by default).
export function runControl({ kind, name, params = {}, label, dryRun = false, note, onDone }) {
  const box = h("div", { class: "runctl" });
  const dry = dryRun ? h("input", { type: "checkbox", checked: true, "aria-label": `DryRun for ${name}` }) : null;
  const btn = h("button", { class: "secondary", onclick: () => prepare() }, label || `Prepare ${name}`);
  const panel = h("div", { class: "runpanel", hidden: true });
  fill(box, h("div", { class: "runrow" }, btn, dry ? h("label", { class: "small" }, dry, " DryRun") : null,
    note ? h("span", { class: "small muted" }, note) : null), panel);

  async function prepare() {
    if (!state.config) { alertIn(panel, "Save a configuration first (Configure tab)."); return; }
    const p = { ...params };
    if (dry) p.DryRun = dry.checked;
    let run;
    try {
      run = await api("/api/runs", { method: "POST", body: { config: state.config, kind, name, params: p } });
    } catch (err) { alertIn(panel, err.message); return; }
    showPrepared(run);
  }

  function showPrepared(run) {
    panel.hidden = false;
    const req = run.request;
    const directRun = req.pipeline === "direct";
    const rows = Object.entries(req.parameters).filter(([k]) => !(directRun && k === "steps"))
      .map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
    const approve = h("input", { type: "checkbox", id: `ap-${run.id}` });
    const typed = h("input", { type: "text", class: "typed", placeholder: run.cluster, "aria-label": "Type the cluster name", autocomplete: "off" });
    const go = h("button", { class: "primary", onclick: () => queue(run, approve.checked, typed.value) },
      run.changes_state ? "Approve and run" : "Run (read-only)");
    fill(panel,
      h("p", {}, h("strong", {}, req.pipeline === "jfrog" ? "JFrog write" : directRun ? `Direct ${req.parameters.plan}` : `Pipeline ${req.pipeline}`),
        req.branch ? ` on ${req.branch}` : "", " ",
        run.changes_state ? h("span", { class: "pill fail" }, "changes state") : h("span", { class: "pill pass" }, "dry run")),
      table(["parameter", "value"], rows),
      directRun ? table(["#", "step", "", "command or request"], req.parameters.steps.map((st, i) => [String(i + 1), st.title,
        h("span", { class: `pill ${st.kind === "change" ? "warn" : "pass"}` }, st.kind),
        h("span", {}, h("code", { class: "cmd" }, st.command), st.dry_run_note ? h("span", { class: "small muted" }, ` (${st.dry_run_note})`) : null)])) : null,
      h("p", { class: "small muted mono" }, `request sha256 ${run.request_sha256}`,
        req.values_sha256 ? ` · values sha256 ${req.values_sha256.slice(0, 16)}…` : ""),
      run.warnings.length ? h("ul", { class: "small warnlist" }, run.warnings.map((w) => h("li", {}, w))) : null,
      run.guards.length
        ? h("div", { class: "callout fail" }, h("strong", {}, "Refused. "), run.guards.join(" "))
        : h("div", { class: "approve" },
          run.changes_state ? h("label", {}, approve, " I approve this change to ", h("strong", {}, run.cluster)) : null,
          run.needs_typed_cluster ? h("label", {}, "Destructive: type the cluster name ", typed) : null, go));
  }

  async function queue(run, approved, typedCluster) {
    let r;
    try {
      r = await api(`/api/runs/${run.id}/approve`, { method: "POST",
        body: { request_sha256: run.request_sha256, approved, typed_cluster: typedCluster } });
    } catch (err) { panel.append(h("p", { class: "error small" }, err.message)); return; }
    const log = h("ol", { class: "events small" });
    const status = pill(r.status);
    fill(panel, h("p", {}, h("strong", {}, `Run ${run.id} `), status), log);
    try {
      await stream(`/api/runs/${run.id}/events`, (kind, data) => {
        if (kind === "event") log.append(h("li", {}, h("span", { class: "mono muted" }, data.t.slice(11, 19), " "), data.text));
        if (kind === "done") {
          status.replaceWith(pill(data.status));
          if (data.error) panel.append(h("p", { class: "error small" }, data.error));
          if (data.result && data.result.steps) panel.append(table(["step", "result", "detail"],
            data.result.steps.map((st) => [st.id, pill(st.status === "done" ? "succeeded" : st.status === "ok" ? "succeeded" : st.status), st.detail])));
          if (onDone) onDone(data);
          state.emit("runs");
        }
      });
    } catch (err) { panel.append(h("p", { class: "error small" }, err.message)); }
  }
  return box;
}

function alertIn(panel, msg) {
  panel.hidden = false;
  fill(panel, h("p", { class: "error small" }, msg));
}

// The three-layer table, shared by Nodes and Simulate.
export function layersView(tl) {
  if (!tl) return [h("p", { class: "muted" }, "No values file.")];
  const fields = ["artifactory_url", "match_images", "provider_name", "default_cache_duration"];
  const fmt = (v) => (Array.isArray(v) ? v.join(", ") : v ?? "—");
  return tl.nodes.map((n) => {
    const bad = new Set(n.problems.map((p) => `${p.layer}:${p.field}`));
    const names = Object.keys(n.layers);
    return h("div", { class: "card" },
      h("p", {}, h("strong", {}, n.node || "no node inspected yet"), n.reason ? h("span", { class: "muted small" }, ` (${n.reason})`) : null,
        " ", pill(n.status)),
      h("table", { class: "grid small layers" },
        h("thead", {}, h("tr", {}, h("th", {}, "field"), names.map((x) => h("th", {}, x)))),
        h("tbody", {}, fields.map((f) => h("tr", {}, h("td", { class: "k" }, f),
          names.map((x) => h("td", { class: bad.has(`${x}:${f}`) ? "bad" : "" }, fmt(n.layers[x][f]))))),
          h("tr", {}, h("td", { class: "k" }, "plugin token URL"),
            h("td", { colspan: String(names.length), class: n.plugin.schemes > 1 ? "bad" : "" }, n.plugin.newest_token_url || "no line yet")),
          n.kubelet_started ? h("tr", {}, h("td", { class: "k" }, "kubelet start / config mtime"),
            h("td", { colspan: String(names.length), class: n.kubelet_older_than_config ? "bad" : "" },
              `${n.kubelet_started} / ${n.config_mtime}`)) : null)),
      n.problems.length ? h("ul", { class: "small" }, n.problems.map((p) => h("li", {}, h("strong", {}, `${p.layer} · ${p.field}: `),
        `${fmt(p.have)} (want ${fmt(p.want)}). `, p.fix))) : null);
  });
}

export function findingView(f, isTop, withRun = true) {
  const sc = f.next && f.next.scenario;
  return h("div", { class: `finding ${f.level}${isTop ? " top" : ""}` },
    h("div", { class: "head" }, h("strong", {}, f.title), h("span", { class: "rule" }, f.rule),
      f.label ? h("span", { class: "tag" }, f.label) : null, h("span", { class: "score" }, `score ${f.score}`)),
    f.hint ? h("p", { class: "small" }, f.hint) : null,
    f.where ? h("p", { class: "small muted" }, "Look at: ", rich(f.where)) : null,
    f.evidence.length ? h("pre", {}, f.evidence.join("\n")) : null,
    sc ? h("div", { class: "next" }, "Next: ",
      withRun && state.config ? runControl({ kind: "pilot", name: sc, dryRun: f.next.dry_run && scenarioDry(sc), label: `Prepare ${sc}` })
        : h("code", {}, sc)) : null);
}

export function scenarioDry(name) {
  const s = (state.knowledge.PILOT_SCENARIOS.concat(state.knowledge.CONSOLE_SCENARIOS)).find((x) => x.name === name);
  return !!(s && s.dry_run);
}

// A list of actions as numbered steps: status from this config's newest run of each, one line
// of explanation, and the run control. items: {title, line, kind, name, params, dryRun, label}.
export async function stepList(items) {
  let runs = [];
  try { runs = (await api("/api/runs")).runs.filter((r) => r.config === state.config); } catch { /* none */ }
  const last = (it) => runs.find((r) => r.kind === it.kind && r.name === it.name);
  return h("ol", { class: "rail actions" }, items.map((it, i) => {
    const r = it.name ? last(it) : null;
    const ok = r && r.status === "succeeded" && r.request.parameters.DryRun !== true;
    return h("li", { class: `rail-step ${ok ? "is-done" : ""}`, title: it.line || "" },
      h("div", { class: "act-head" }, h("span", { class: "step-n" }, String(i + 1)),
        h("div", { class: "act-text" }, h("b", {}, it.title)),
        r ? h("span", { class: "act-last small muted" }, pill(r.status), ` ${r.request.parameters.DryRun === true ? "dry run " : ""}${r.created.slice(0, 16).replace("T", " ")}`) : null),
      it.body || null,
      it.name ? runControl({ kind: it.kind, name: it.name, params: it.params || {}, dryRun: it.dryRun ?? (it.kind === "pilot" && scenarioDry(it.name)),
        label: it.label || "Run" }) : null);
  }));
}

// A page header with optional actions on the right.
export function pageHead(title, ...actions) {
  return h("div", { class: "cfg-head" }, h("div", {}, h("h1", {}, title)),
    actions.length ? h("div", { class: "head-actions" }, actions) : null);
}
