// Runs (/runs), in the Azure portal's resource style like Home: title and command bar (Refresh, Delete
// selected, Clear finished), Essentials, tabs by area (All, Serverless, Kubernetes, Infrastructure), then
// one filter bar ("Filter for any field…", Cloud, Type, Status) over one table grouped by cloud. Filters
// live in the address. Each row opens its run. Data: data.js.
import { CLOUD_NAMES, LIVE_STATES, h, markEl, when, runPill, loadAll, removeRun } from "./data.js";

const $ = (id) => document.getElementById(id);
const TABS = ["", "serverless", "kubernetes", "infrastructure"];
const TAB_ID = (t) => `tab-${t || "all"}`;
const u0 = new URLSearchParams(location.search);
const F = { area: TABS.includes(u0.get("area")) ? u0.get("area") : "", q: u0.get("q") || "", cloud: u0.get("cloud") || "",
  type: u0.get("type") || "", status: u0.get("status") || "" };
let all = [], shown = [];
const picked = new Set();  // run api paths
let pending = null;  // runs awaiting the inline Delete/Keep answer

const FAILED = ["failed", "cancelled", "refused", "interrupted"];
const STATUS = { active: (s) => LIVE_STATES.has(s), succeeded: (s) => s === "succeeded", failed: (s) => FAILED.includes(s) };
const typeOf = (r) => String(r.type || "").split(" · ")[0];
const DOT = { ok: "ok", warn: "warn", bad: "fail", info: "warn" };

function saveQuery() {
  const u = new URLSearchParams(Object.entries(F).filter(([, v]) => v));
  history.replaceState(null, "", `/runs${u.toString() ? `?${u}` : ""}`);
}

function row(r) {
  const [cls, text] = runPill(r.status);
  const live = LIVE_STATES.has(r.status);
  const box = h("input", { type: "checkbox", "aria-label": `Select run of ${r.name}`, disabled: live || null,
    title: live ? "In progress: cancel it first" : null });
  box.checked = picked.has(r.api);
  box.addEventListener("change", () => { box.checked ? picked.add(r.api) : picked.delete(r.api); sync(); });
  const tr = h("tr", { class: r.href ? "row-link" : null },
    h("td", { class: "sel" }, box),
    h("td", {}, h("span", { class: "blade-name" }, r.mark ? markEl(r.mark) : null, r.href ? h("a", { href: r.href, text: r.name }) : h("span", { text: r.name }))),
    h("td", {}, h("span", { class: `blade-state ${DOT[cls] || ""}`, text })),
    h("td", {}, r.typeHref ? h("a", { href: r.typeHref, text: r.type }) : r.type),
    h("td", {}, r.href ? h("a", { href: r.href, text: when(r.created) }) : when(r.created)));
  if (r.href) tr.addEventListener("click", (e) => { if (!e.target.closest("a, input, .sel")) location.assign(r.href); });
  return tr;
}

const inTab = () => all.filter((r) => !F.area || r.area === F.area);

// Tabs, pills (their options follow the tab's rows), then the rows.
function draw() {
  for (const t of TABS) {
    const b = $(TAB_ID(t));
    b.setAttribute("aria-selected", String(t === F.area));
    b.tabIndex = t === F.area ? 0 : -1;
    const n = t ? all.filter((r) => r.area === t).length : all.length;
    b.textContent = `${t ? b.dataset.label : "All"}${all.length ? ` (${n})` : ""}`;
  }
  $("runs-panel").setAttribute("aria-labelledby", TAB_ID(F.area));
  const rows = inTab();
  const clouds = [...new Set(rows.map((r) => r.cloud).filter(Boolean))];
  const types = [...new Set(rows.map(typeOf).filter(Boolean))].sort();
  const set = (k) => (v) => { F[k] = v; saveQuery(); draw(); };
  $("runs-filters").replaceChildren(window.consoleBlade.bar({ q: F.q, onQuery: (v) => { F.q = v; saveQuery(); render(); }, pills: [
    { label: "Cloud", value: F.cloud, options: clouds.map((c) => [c, CLOUD_NAMES[c] || c]), onChange: set("cloud") },
    { label: "Type", value: F.type, options: types.map((t) => [t, t]), onChange: set("type") },
    { label: "Status", value: F.status, options: [["active", "In progress"], ["succeeded", "Succeeded"], ["failed", "Failed"]], onChange: set("status") },
  ] }));
  render();
}

function render() {
  const q = F.q.trim().toLowerCase();
  const rows = inTab();
  shown = rows.filter((r) => (!F.cloud || r.cloud === F.cloud) && (!F.type || typeOf(r) === F.type) && (!F.status || STATUS[F.status]?.(r.status))
    && (!q || [r.name, r.type, CLOUD_NAMES[r.cloud], runPill(r.status)[1]].some((v) => String(v || "").toLowerCase().includes(q))));
  // One block per cloud, never mixed: Azure, then Google Cloud, then runs with no cloud recorded.
  const order = ["azure", "gcp", ...new Set(shown.map((r) => r.cloud || null).filter((c) => !["azure", "gcp"].includes(c)))];
  const groups = order.map((c) => [c, shown.filter((r) => (r.cloud || null) === c)]).filter(([, xs]) => xs.length);
  const head = (c, n) => {
    const logo = h("span", { class: "home-group-logo" });
    logo.innerHTML = (c && window.consoleNav?.icon(c)) || "";   // constant SVG only
    return h("tr", { class: "home-group" }, h("th", { scope: "colgroup", colspan: "5" },
      h("span", { class: "home-group-name" }, logo, CLOUD_NAMES[c] || "No cloud recorded", h("small", { text: String(n) }))));
  };
  for (const t of document.querySelectorAll(".runs-rows-more")) t.remove();
  const [first, ...rest] = groups;
  // A cloud heading only when more than one cloud is listed: alone, it repeats the filter.
  // Several clouds: each gets its own column header row (with its own select-all) under its heading;
  // the shared header row hides.
  const many = groups.length > 1;
  $("runs-all").closest("thead").hidden = many;
  const cols = (c, xs) => {
    const box = h("input", { type: "checkbox", class: "cloud-all", "aria-label": `Select all finished ${CLOUD_NAMES[c] || "other"} runs shown` });
    box.addEventListener("change", () => { for (const r of finished(xs)) box.checked ? picked.add(r.api) : picked.delete(r.api); render(); });
    box._runs = xs;
    return h("tr", { class: "cloud-cols" }, h("th", { scope: "col", class: "sel" }, box),
      ...["Name", "Status", "Type", "Started"].map((t) => h("th", { scope: "col", text: t })));
  };
  const top = (c, xs) => (many ? [head(c, xs.length), cols(c, xs)] : []);
  $("runs-rows").replaceChildren(...(first ? [...top(first[0], first[1]), ...first[1].map(row)] : []));
  $("runs-rows").after(...rest.map(([c, xs]) => h("tbody", { class: "runs-rows-more" }, ...top(c, xs), ...xs.map(row))));
  $("runs-count").textContent = `Showing ${shown.length} of ${rows.length} records.`;
  $("runs-none").hidden = shown.length > 0;
  $("runs-none").textContent = rows.length ? "No run matches the filters" : "No runs yet";
  sync();
}

// Essentials: label : value, two columns; a value with a filtered view links to it.
function essentials() {
  const n = (f) => all.filter((r) => f(r.status)).length;
  const last = all[0], oldest = all[all.length - 1];
  const pair = (label, value, href) => [h("div", { class: "ess-pair" }, h("dt", { text: label }), h("dd", {}, href ? h("a", { href, text: value }) : value))];
  $("ess-left").replaceChildren(
    ...pair("Runs kept", String(all.length)),
    ...pair("Oldest kept", oldest ? when(oldest.created) : "—"));
  $("ess-right").replaceChildren(
    ...pair("In progress", String(n(STATUS.active)), n(STATUS.active) ? "/runs?status=active" : null),
    ...pair("Succeeded / failed", `${n(STATUS.succeeded)} / ${n(STATUS.failed)}`, n(STATUS.failed) ? "/runs?status=failed" : null),
    ...pair("Last run", last ? `${last.name} · ${runPill(last.status)[1]} · ${when(last.created)}` : "—", last?.href || null));
}

const finished = (rows) => rows.filter((r) => !LIVE_STATES.has(r.status) && r.api);
// Buttons and the select-all box follow the selection; a selection hidden by a filter is not acted on.
function sync() {
  const visible = new Set(finished(shown).map((r) => r.api));
  for (const p of [...picked]) if (!visible.has(p)) picked.delete(p);
  $("runs-delete").disabled = !picked.size;
  $("runs-delete").querySelector("span").textContent = picked.size ? `Delete selected (${picked.size})` : "Delete selected";
  $("runs-clear").disabled = !visible.size;
  const box = $("runs-all");
  box.checked = visible.size > 0 && picked.size === visible.size;
  box.indeterminate = picked.size > 0 && picked.size < visible.size;
  box.disabled = !visible.size;
  for (const b of document.querySelectorAll("input.cloud-all")) {
    const mine = finished(b._runs).map((r) => r.api), n = mine.filter((a) => picked.has(a)).length;
    b.checked = mine.length > 0 && n === mine.length;
    b.indeterminate = n > 0 && n < mine.length;
    b.disabled = !mine.length;
  }
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
  $("runs-count").textContent = "Loading…";
  const { runs, errors, signedOut } = await loadAll();
  $("session-state").hidden = !signedOut;
  $("session-state").textContent = "Not signed in";
  const bad = errors.filter((e) => e.message !== "signed-out");
  $("runs-errors").hidden = !bad.length;
  $("runs-errors").textContent = bad.map((e) => `${e.source}: ${e.message}`).join(" · ");
  all = runs;
  essentials();
  draw();
}

for (const t of TABS) {
  const b = $(TAB_ID(t));
  b.dataset.label = b.textContent;
  b.addEventListener("click", () => { Object.assign(F, { area: t, cloud: "", type: "", status: "" }); saveQuery(); draw(); });
}
$("runs-panel").previousElementSibling.addEventListener("keydown", (e) => {
  const i = TABS.indexOf(F.area), d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
  if (!d) return;
  $(TAB_ID(TABS[(i + d + TABS.length) % TABS.length])).click();
  $(TAB_ID(F.area)).focus();
});
$("ess-h").addEventListener("click", () => {
  const open = $("ess-h").getAttribute("aria-expanded") !== "true";
  $("ess-h").setAttribute("aria-expanded", String(open));
  $("ess-body").hidden = !open;
});
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
saveQuery();
draw();
load();
