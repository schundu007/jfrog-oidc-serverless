// Runs (/runs): every run of every workload and cluster, newest first, filtered by cloud, status and
// text. Each row opens the run's own page. Data: data.js.
import { CLOUD_NAMES, LIVE_STATES, h, markEl, when, runPill, loadAll } from "./data.js";

const $ = (id) => document.getElementById(id);
let all = [];

const STATUS = { active: (s) => LIVE_STATES.has(s), succeeded: (s) => s === "succeeded",
  failed: (s) => ["failed", "cancelled", "refused"].includes(s) };

function row(r) {
  const [cls, text] = runPill(r.status);
  const name = r.href ? h("a", { href: r.href, text: r.name }) : h("span", { text: r.name });
  const tr = h("tr", { class: r.href ? "row-link" : null },
    h("td", {}, h("span", { class: `pill ${cls}`, text })),
    h("th", { scope: "row" }, h("span", { class: "run-what" }, r.mark ? markEl(r.mark) : null, name)),
    h("td", { class: "small", text: r.type }),
    h("td", { class: "small", text: CLOUD_NAMES[r.cloud] || "—" }),
    h("td", { class: "small mono", text: when(r.created) }));
  if (r.href) tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.assign(r.href); });
  return tr;
}

function render() {
  const cloud = $("runs-cloud").value, status = $("runs-status").value, q = $("runs-filter").value.trim().toLowerCase();
  const shown = all.filter((r) => (!cloud || r.cloud === cloud) && (!status || STATUS[status](r.status))
    && (!q || [r.name, r.type].some((v) => String(v).toLowerCase().includes(q))));
  $("runs-rows").replaceChildren(...shown.map(row));
  $("runs-none").hidden = shown.length > 0;
  $("runs-count").textContent = all.length ? String(all.length) : "";
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

for (const id of ["runs-cloud", "runs-status"]) $(id).addEventListener("change", render);
$("runs-filter").addEventListener("input", render);
$("runs-refresh").addEventListener("click", load);
load();
