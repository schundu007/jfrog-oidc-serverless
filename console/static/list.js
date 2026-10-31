// Serverless (/<cloud>, or one kind with ?type=): the resources that exist in the cloud right now, then
// Create tiles grouped by what the workload does. A configuration is listed only once the cloud
// confirms its workload exists (wired or not); missing or unreadable ones are not shown. The whole row opens the workload; "⋯" holds Delete (saved only: an
// onboarded config lives in git, where only offboarding removes it).

import { $, api, h, fill, inSection, ofCloud, href, CLOUD, TYPE, template, ago, stamp, help, status as setStatus } from "./core.js";
import { kindLabel } from "./configure.js";

const ls = { rows: [], schema: null, checked: false };
const REAL = new Set(["wired", "not_wired"]);
// Status dot: green when the JFrog settings are on the workload, amber when it exists without them.
const DOT = { wired: ["ok", "Exists, wired to JFrog"], not_wired: ["warn", "Exists, not wired to JFrog"] };

// What a workload kind does, as the clouds' own consoles group them; and its service icon.
const GROUPS = [["Services", (k) => /_service$|_container_app$/.test(k)],
  ["Jobs and worker pools", (k) => /_job$|_worker_pool$/.test(k)],
  ["Functions", (k) => /function/.test(k)]];
const iconOf = (k) => (k === "gcp_run_function" ? "fn" : k.startsWith("gcp_") ? "run" : k.startsWith("azure_functions") ? "func" : "aca");

function row(c) {
  const open = href.workload(c.name);
  const [tone, label] = DOT[c.live.state];
  const tr = h("tr", { class: "row-link", "data-name": c.name },
    h("td", { class: "col-icon" }, h("span", { class: `ov-dot ov-dot-${tone}`, role: "img", "aria-label": label, title: label })),
    h("td", {}, h("a", { href: open, text: c.name })),
    h("td", { text: c.where || "—" }),
    h("td", { text: ls.schema?.kinds?.[c.kind]?.label || c.kind }),
    h("td", { title: c.when ? stamp(c.when) : null, text: c.when ? ago(c.when) : "—" }));
  tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.assign(open); });
  return tr;
}

function render() {
  const real = ls.checked ? ls.rows.filter((c) => REAL.has(c.live?.state)) : [];
  fill($("wl-rows"), real.map(row));
  $("wl-empty").hidden = !ls.checked || real.length > 0;
}

function tiles() {
  const kinds = Object.keys(ls.schema?.kinds || {}).filter((k) => ofCloud(k) && (!TYPE || k === TYPE));
  return GROUPS.map(([title, test]) => [title, kinds.filter(test)]).filter(([, ks]) => ks.length).map(([title, ks]) =>
    h("section", { class: "ov-group" }, h("h2", { class: "ov-group-title", text: title }),
      h("div", { class: "ov-tiles" }, ks.map((k) => {
        const a = h("a", { class: "ov-tile", href: `${href.create()}?kind=${encodeURIComponent(k)}` });
        const ico = h("span", { class: "ov-tile-icon" });
        ico.innerHTML = window.consoleNav?.icon(iconOf(k)) || "";   // constant SVG only
        a.append(ico, h("span", { text: ls.schema.kinds[k].label }));
        return a;
      }))));
}

async function load() {
  const [{ configs }, { runs }] = await Promise.all([api("/api/configs"), api("/api/runs")]);
  const mine = runs.filter((r) => inSection(r.kind));
  ls.rows = configs.filter((c) => inSection(c.kind)).map((c) => {
    const run = mine.find((r) => r.name === c.name);
    return { ...c, when: run?.created || c.updated || "" };
  });
  ls.checked = false;
  render();
  setStatus($("list-status"), "Checking the cloud", "busy");
  try {
    const { status } = await api(`/api/live?cloud=${CLOUD}`);
    for (const c of ls.rows) c.live = status[c.name] || { state: "unknown" };
    setStatus($("list-status"), "");
  } catch (e) {
    for (const c of ls.rows) c.live = { state: "unknown" };
    setStatus($("list-status"), `Could not read the cloud: ${e.message}`, "error");
  }
  ls.checked = true;
  render();
}

export async function mountList(page, { schema }) {
  ls.schema = schema;
  page.replaceChildren(template("tpl-list"));
  // Titled after its menu item (one service, e.g. Cloud Run jobs), or Serverless for all of the cloud's.
  const section = window.consoleNav?.section(TYPE || `serverless-${CLOUD}`);
  if (section) {
    page.querySelector(".page-header h1").textContent = section.label;
    document.title = `${section.label} · ${section.cloud}`;
  }
  $("wl-help").append(help("Workloads onboarded here that exist in the cloud now. Green: wired to JFrog; amber: not wired yet."));
  $("wl-where").textContent = CLOUD === "azure" ? "Resource group" : "Region";
  fill($("wl-create"), tiles());
  $("wl-refresh").addEventListener("click", () => load().catch((e) => setStatus($("list-status"), e.message, "error")));
  try {
    await load();
  } catch (e) {
    setStatus($("list-status"), `Could not list workloads: ${e.message}`, "error");
  }
}
