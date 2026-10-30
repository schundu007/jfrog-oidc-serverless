// Security: the review rows with a recorded status and note (saved per config), the PRD gate,
// the automated checks, and the open items.

import { api, h, fill, state } from "./app.js";
import { $, table, pill, needConfig, pageHead } from "./ui.js";

export function initSecurity() {
  state.on("config", render);
  state.on("runs", render);
}

async function save(patch) {
  await api(`/api/status/${encodeURIComponent(state.config)}`, { method: "PUT", body: { status: patch } });
  render();
}

async function render() {
  const P = $("panel-security");
  if (needConfig(P)) return;
  const k = state.knowledge, sr = k.SECURITY_REVIEW;
  const name = encodeURIComponent(state.config);
  const [{ status }, a, { config }] = await Promise.all([api(`/api/status/${name}`), api(`/api/nodes?config=${name}`), api(`/api/configs/${name}`)]);
  const sec = status.security || {}, items = status.open_items || {};
  const prd = config.cluster && config.cluster.environment === "PRD";
  const gateOpen = sr.rows.filter((r) => r.prd_gate && (sec[r.id] || {}).status !== "decided");
  const decided = sr.rows.filter((r) => (sec[r.id] || {}).status === "decided").length;
  fill(P,
    pageHead("Security", `${decided}/${sr.rows.length} review items decided.`),
    gateOpen.length ? h("div", { class: `outcome ${prd ? "is-fail" : "is-warn"}` },
      h("div", {}, h("b", {}, prd ? "PRD blocked. " : "PRD gate: "), `decide ${gateOpen.map((r) => r.area).join(" and ")} before any PRD run.`)) : null,
    h("h2", {}, "Checks"),
    table(["check", "result", "detail"], [
      ["Chart RBAC", pill(a.chart_rbac.status), a.chart_rbac.detail],
      ["Secrets in stored logs", pill(a.log_leaks.status), a.log_leaks.detail]]),
    h("h2", {}, "Review"),
    table(["area", "finding", "PRD gate", "status", "note"], sr.rows.map((r) => {
      const cur = sec[r.id] || {};
      return [h("b", {}, r.area), r.finding, r.prd_gate ? "yes" : "",
        h("select", { "aria-label": `status of ${r.area}`, onchange: (e) => save({ security: { [r.id]: { ...cur, status: e.target.value } } }) },
          sr.statuses.map((s) => h("option", { selected: (cur.status || "open") === s }, s))),
        h("input", { type: "text", value: cur.note || "", "aria-label": `note for ${r.area}`, spellcheck: "false",
          onchange: (e) => save({ security: { [r.id]: { ...cur, note: e.target.value } } }) })];
    })),
    h("h2", {}, "Open items"),
    table(["done", "item", "note"], k.OPEN_ITEMS.map((o) => {
      const cur = items[o.id] || {};
      return [h("input", { type: "checkbox", checked: cur.status === "done", "aria-label": o.title,
        onchange: (e) => save({ open_items: { [o.id]: { ...cur, status: e.target.checked ? "done" : "open" } } }) }),
        h("span", {}, h("b", {}, o.title), h("br"), h("span", { class: "small muted" }, o.check || "")),
        h("input", { type: "text", value: cur.note || "", "aria-label": `note for ${o.title}`,
          onchange: (e) => save({ open_items: { [o.id]: { ...cur, note: e.target.value } } }) })];
    })));
}
