// Runs (/runs): every run of every workload, cluster and infrastructure stack, newest first, filtered
// by text and the Cloud, Area and Status pills (kept in the address). Each row opens its run. Data: data.js.
import { CLOUD_NAMES, LIVE_STATES, h, markEl, when, runPill, loadAll, removeRun } from "./data.js";

const $ = (id) => document.getElementById(id);
let all = [], shown = [];
const picked = new Set();  // run api paths
let pending = null;  // runs awaiting the inline Delete/Keep answer
const F = Object.fromEntries(["q", "cloud", "area", "status"].map((k) => [k, new URLSearchParams(location.search).get(k) || ""]));
const AREAS = [["serverless", "Serverless"], ["kubernetes", "Kubernetes"], ["infrastructure", "Infrastructure"]];

const STATUS = { active: (s) => LIVE_STATES.has(s), succeeded: (s) => s === "succeeded",
  failed: (s) => ["failed", "cancelled", "refused"].includes(s) };

function row(r) {
  const [cls, text] = runPill(r.status);
  const name = r.href ? h("a", { href: r.href, text: r.name }) : h("span", { text: r.name });
  const live = LIVE_STATES.has(r.status);
  const box = h("input", { type: "checkbox", "aria-label": `Select run of ${r.name}`, disabled: live || null,
    title: live ? "In progress: cancel it first" : null });
  box.checked = picked.has(r.api);
  box.addEventListener("change", () => { box.checked ? picked.add(r.api) : picked.delete(r.api); sync(); });
  const tr = h("tr", { class: r.href ? "row-link" : null },
    h("td", { class: "sel" }, box),
    h("td", {}, h("span", { class: `pill ${cls}`, text })),
    h("th", { scope: "row" }, h("span", { class: "run-what" }, r.mark ? markEl(r.mark) : null, name)),
    h("td", { class: "small", text: r.type }),
    h("td", { class: "small", text: CLOUD_NAMES[r.cloud] || "—" }),
    h("td", { class: "small mono", text: when(r.created) }));
  if (r.href) tr.addEventListener("click", (e) => { if (!e.target.closest("a, input, .sel")) location.assign(r.href); });
  return tr;
}

function saveQuery() {
  const u = new URLSearchParams(Object.entries(F).filter(([, v]) => v));
  history.replaceState(null, "", `/runs${u.toString() ? `?${u}` : ""}`);
}

function filters() {
  const set = (k) => (v) => { F[k] = v; saveQuery(); filters(); render(); };
  $("runs-filters").replaceChildren(window.consoleBlade.bar({ q: F.q, onQuery: (v) => { F.q = v; saveQuery(); render(); }, pills: [
    { label: "Cloud", value: F.cloud, options: [["gcp", "Google Cloud"], ["azure", "Azure"]], onChange: set("cloud") },
    { label: "Area", value: F.area, options: AREAS, onChange: set("area") },
    { label: "Status", value: F.status, options: [["active", "In progress"], ["succeeded", "Succeeded"], ["failed", "Failed"]], onChange: set("status") },
  ] }));
}

function render() {
  const q = F.q.trim().toLowerCase();
  shown = all.filter((r) => (!F.cloud || r.cloud === F.cloud) && (!F.area || r.area === F.area) && (!F.status || STATUS[F.status]?.(r.status))
    && (!q || [r.name, r.type, CLOUD_NAMES[r.cloud]].some((v) => String(v || "").toLowerCase().includes(q))));
  $("runs-rows").replaceChildren(...shown.map(row));
  $("runs-none").hidden = shown.length > 0;
  $("runs-count").textContent = all.length ? String(all.length) : "";
  sync();
}

const finished = (rows) => rows.filter((r) => !LIVE_STATES.has(r.status) && r.api);
// Buttons and the select-all box follow the selection; a selection hidden by a filter is not acted on.
function sync() {
  const visible = new Set(finished(shown).map((r) => r.api));
  for (const p of [...picked]) if (!visible.has(p)) picked.delete(p);
  $("runs-delete").disabled = !picked.size;
  $("runs-delete").textContent = picked.size ? `Delete selected (${picked.size})` : "Delete selected";
  $("runs-clear").disabled = !visible.size;
  const box = $("runs-all");
  box.checked = visible.size > 0 && picked.size === visible.size;
  box.indeterminate = picked.size > 0 && picked.size < visible.size;
  box.disabled = !visible.size;
}

function ask(rows, what) {
  pending = rows;
  $("runs-confirm-text").textContent = `${what}: ${rows.length} ${rows.length === 1 ? "run" : "runs"}, records and logs. This cannot be undone.`;
  $("runs-confirm").hidden = false;
  $("runs-confirm-no").focus();
}

async function confirmDelete() {
  const rows = pending || [];
  pending = null;
  $("runs-confirm").hidden = true;
  for (const b of ["runs-delete", "runs-clear", "runs-confirm-yes"]) $(b).disabled = true;
  const res = await Promise.allSettled(rows.map((r) => removeRun(r.api)));
  const failed = res.filter((x) => x.status === "rejected");
  picked.clear();
  $("runs-done").hidden = false;
  $("runs-done").textContent = failed.length
    ? `Deleted ${rows.length - failed.length} of ${rows.length}. Not deleted: ${[...new Set(failed.map((f) => f.reason?.message))].join("; ")}`
    : `Deleted ${rows.length} ${rows.length === 1 ? "run" : "runs"}.`;
  $("runs-confirm-yes").disabled = false;
  await load();
}

async function load() {
  const { runs, errors, signedOut } = await loadAll();
  $("session-state").hidden = !signedOut;
  $("session-state").textContent = "Not signed in";
  const shown = errors.filter((e) => e.message !== "signed-out");
  $("runs-errors").hidden = !shown.length;
  $("runs-errors").textContent = shown.map((e) => `${e.source}: ${e.message}`).join(" · ");
  all = runs;
  render();
}

filters();
$("runs-refresh").addEventListener("click", load);
$("runs-all").addEventListener("change", (e) => {
  picked.clear();
  if (e.target.checked) for (const r of finished(shown)) picked.add(r.api);
  render();
});
$("runs-delete").addEventListener("click", () => ask(finished(shown).filter((r) => picked.has(r.api)), "Delete selected"));
$("runs-clear").addEventListener("click", () => ask(finished(shown), "Clear finished runs shown"));
$("runs-confirm-yes").addEventListener("click", confirmDelete);
$("runs-confirm-no").addEventListener("click", () => { pending = null; $("runs-confirm").hidden = true; });
load();
