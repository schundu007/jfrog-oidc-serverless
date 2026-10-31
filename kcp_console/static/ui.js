// Shared UI helpers: element lookup and the text filters for run output.

export const $ = (id) => document.getElementById(id);

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
