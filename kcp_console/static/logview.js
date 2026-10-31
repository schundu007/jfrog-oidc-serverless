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

// Replace a container's children with the formatted lines.
export function renderLog(el, lines) {
  el.replaceChildren(...(lines || []).map(logLine));
  return el;
}
