// Log lines, formatted: each line is classified (step, error, warning, Terraform resource progress,
// summary, output "key = value", plan diff) and rendered as spans with a class per part, so the CSS
// can color them. Text only: nothing is parsed as HTML.

const CLOCK = /^(\d\d:\d\d:\d\d(?:\.\d+)?)(\s+)/;
const RESOURCE = /^((?:module\.[\w-]+(?:\[[^\]]*\])?\.)*(?:data\.)?[\w-]+\.[\w-]+(?:\[[^\]]*\])?): (.*)$/;
const VERB = [
  [/^(Creating|Creation complete)/, "add"],
  [/^(Modifying|Modifications complete)/, "change"],
  [/^(Destroying|Destruction complete)/, "remove"],
  [/^(Still \w+|Reading|Read complete|Refreshing state)/, "quiet"],
];
const ERROR = /(^│?\s*Error\b|\berror:|\bfailed\b|\bFailed\b|\bdenied\b|\bforbidden\b|^Traceback)/;
const WARN = /(^│?\s*Warning\b|\bwarning:)/;
const SUMMARY = /^(Apply complete!|Destroy complete!|Plan:|No changes\.|Deployed\.|Destroyed\.)/;

const span = (cls, text) => {
  const s = document.createElement("span");
  if (cls) s.className = cls;
  s.textContent = text;
  return s;
};

// "<address>: Modifying... [id=…]" → address, verb, rest (the [id=…] muted).
function resourceParts(addr, rest) {
  const kind = VERB.find(([re]) => re.test(rest))?.[1];
  if (!kind) return null;
  const id = rest.search(/\s\[id=/);
  const head = id >= 0 ? rest.slice(0, id) : rest;
  return [span("lg-addr", addr), span("", ": "), span(`lg-${kind}`, head), id >= 0 ? span("lg-dim", rest.slice(id)) : null];
}

export function logLine(text) {
  const row = document.createElement("span");
  row.className = "lg";
  let t = String(text ?? "");
  const clock = t.match(CLOCK);
  if (clock) { row.append(span("lg-time", clock[1]), clock[2]); t = t.slice(clock[0].length); }

  let kind = "";
  let parts = null;
  if (t.startsWith("▶")) {
    row.classList.add("lg-step");
    const body = t.replace(/^▶\s*/, "");
    const sub = ERROR.test(body) ? "error" : /^(Deployed|Destroyed|Applied|Done)\b/.test(body) ? "done" : "";
    if (sub) row.classList.add(`lg-${sub}`);
    row.append(span("lg-mark", "▶ "), body);
    return row;
  }
  const res = t.match(RESOURCE);
  const kv = t.match(/^(\s*)([\w.-]+)( = )(.*)$/);
  const ok = t.match(/^(.*?): (ok|done|skipped)(,.*)?$/);
  if (ERROR.test(t)) kind = "error";
  else if (WARN.test(t)) kind = "warn";
  else if (SUMMARY.test(t)) kind = /^(Apply complete|Destroy complete|Deployed|Destroyed)/.test(t) ? "done" : "summary";
  else if (/^Outputs:\s*$/.test(t)) kind = "heading";
  else if (res && (parts = resourceParts(res[1], res[2]))) kind = "resource";
  else if (/^\s*(-\/\+|[+~-])\s/.test(t)) kind = { "+": "add", "~": "change", "-": "remove" }[t.trim()[0]] || "change";
  else if (/^\s*│/.test(t)) kind = "diag";
  else if (kv) { kind = "kv"; parts = [kv[1], span("lg-key", kv[2]), span("lg-dim", kv[3]), span("lg-val", kv[4])]; }
  else if (ok) { kind = "result"; parts = [`${ok[1]}: `, span(`lg-${ok[2]}`, ok[2]), ok[3] ? span("lg-dim", ok[3]) : null]; }
  if (kind) row.classList.add(`lg-${kind}`);
  row.append(...(parts || [t]).filter((p) => p !== null && p !== undefined));
  return row;
}

// ------------------------------------------------------------------ table view: Time | Message

const TIME_KEY = "log-timestamps";
const showTimes = () => { try { return localStorage.getItem(TIME_KEY) !== "off"; } catch { return true; } };
const ISO_LEAD = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s/;

// A row is {t, text}: t is epoch seconds, an ISO string or empty. A line that starts with an ISO
// stamp (pipeline logs) gives its own time.
function toDate(t) {
  if (t === null || t === undefined || t === "") return null;
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  return Number.isNaN(d.getTime()) ? null : d;
}
const hhmmss = (d) => (d ? d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "");

function tableRow(row) {
  let { t, text } = typeof row === "string" ? { t: null, text: row } : row;
  text = String(text ?? "");
  const lead = text.match(ISO_LEAD);
  if (lead) { t = t || lead[1]; text = text.slice(lead[0].length); }
  const line = logLine(text);
  const d = toDate(t);
  const tr = document.createElement("tr");
  tr.className = [...line.classList].filter((c) => c !== "lg").join(" ");
  const time = document.createElement("td");
  time.className = "lt-time";
  time.textContent = hhmmss(d);
  if (d) time.title = d.toLocaleString();
  const msg = document.createElement("td");
  msg.className = "lt-msg";
  msg.append(line);
  tr.append(time, msg);
  return tr;
}

// The table, scrolled to the bottom while new rows arrive (unless the reader scrolled up).
export function logTable(rows = []) {
  const wrap = document.createElement("div");
  wrap.className = "lt-wrap";
  const table = document.createElement("table");
  table.className = "lt" + (showTimes() ? "" : " lt-notime");
  const head = document.createElement("thead");
  const hr = head.insertRow();
  for (const [cls, label] of [["lt-time", "Time"], ["", "Message"]]) {
    const th = document.createElement("th");
    th.scope = "col";
    if (cls) th.className = cls;
    th.textContent = label;
    hr.append(th);
  }
  const body = document.createElement("tbody");
  table.append(head, body);
  wrap.append(table);
  const keep = (fn) => {
    const stick = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 8;
    fn();
    if (stick) wrap.scrollTop = wrap.scrollHeight;
  };
  wrap.set = (next) => keep(() => body.replaceChildren(...(next || []).map(tableRow)));
  wrap.add = (row) => keep(() => body.append(tableRow(row)));
  wrap.count = () => body.rows.length;
  wrap.set(rows);
  return wrap;
}

// "Timestamps" on/off: one setting for every log table, remembered in this browser.
export function timeToggle() {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = showTimes();
  box.onchange = () => {
    try { localStorage.setItem(TIME_KEY, box.checked ? "on" : "off"); } catch { /* not remembered */ }
    document.querySelectorAll("table.lt").forEach((t) => t.classList.toggle("lt-notime", !box.checked));
    document.querySelectorAll("input.lt-toggle").forEach((b) => { b.checked = box.checked; });
  };
  box.className = "lt-toggle";
  const label = document.createElement("label");
  label.className = "lt-toggle-label";
  label.append(box, " Timestamps");
  return label;
}

// A collapsible log: title, the toggle, then the table. box.set(rows) / box.add(row) update it.
export function logBox(title, rows = [], open = true) {
  const box = document.createElement("details");
  box.className = "lt-box";
  box.open = open;
  const summary = document.createElement("summary");
  summary.textContent = title;
  const bar = document.createElement("div");
  bar.className = "lt-bar";
  const count = document.createElement("span");
  count.className = "lt-count";
  bar.append(timeToggle(), count);
  const table = logTable();
  box.append(summary, bar, table);
  const counted = () => { const n = table.count(); count.textContent = `${n} line${n === 1 ? "" : "s"}`; box.hidden = !n; };
  box.set = (next) => { table.set(next); counted(); };
  box.add = (row) => { table.add(row); counted(); };
  box.set(rows);
  return box;
}
