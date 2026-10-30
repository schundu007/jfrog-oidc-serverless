// Shared UI pieces: tables, status text and the event-stream reader.

import { h, rich, state, url } from "./app.js";

export const $ = (id) => document.getElementById(id);

const HUMAN = (k) => String(k).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function table(cols, rows, cls = "grid small") {
  const cell = (v) => (v instanceof Node || Array.isArray(v) ? v : typeof v === "boolean" ? (v ? "yes" : "no") : rich(v ?? ""));
  return h("table", { class: cls }, h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, HUMAN(c))))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v) => h("td", {}, cell(v)))))));
}

export const pill = (status) => h("span", { class: `pill ${String(status).toLowerCase().replace(/[^a-z]/g, "")}` }, String(status));

// Server-sent events over fetch, so the token stays in a header.
export async function stream(path, onFrame) {
  const resp = await fetch(url(path), { headers: { "X-Console-Token": state.token }, credentials: "omit", cache: "no-store" });
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

// The workflow lines of a run: its steps and outcome, never the commands behind them.
export const workflow = (text) => !/^\$ /.test(text) && !/: request [0-9a-f]{6,}/.test(text);

// Short, non-identifying text for the screen: no URLs, IDs or image references, whatever a step or
// an older run recorded.
export const brief = (text) => String(text ?? "")
  .replace(/https?:\/\/\S+/g, "…")
  .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "…")
  .replace(/"?[\w.-]+\.[a-z]{2,}\/[\w./-]+(:[\w.-]+)?"?/gi, "the image")
  .replace(/\s*\(\s*…\s*\)/g, "")
  .slice(0, 160);
