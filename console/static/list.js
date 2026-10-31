// Serverless (/<cloud>, or one kind with ?type=): the workloads that exist in the cloud right now,
// read live, whether onboarded here or not; then type tiles grouped by what a workload does.
// A row opens its workload when onboarded, else New workload on its type.

import { $, api, h, fill, inSection, ofCloud, href, CLOUD, TYPE, template, help, status as setStatus } from "./core.js";
import { kindLabel } from "./configure.js";

const ls = { rows: [], configs: [], schema: null, checked: false };
// Status dot: green when the workload is onboarded to JFrog OIDC here, grey when it only exists in the cloud.
const DOT = { true: ["ok", "Onboarded to JFrog OIDC"], false: ["none", "Not onboarded"] };

// What a workload kind does, as the clouds' own consoles group them; and its service icon.
const GROUPS = [["Services", (k) => /_service$|_container_app$/.test(k)],
  ["Jobs and worker pools", (k) => /_job$|_worker_pool$/.test(k)],
  ["Functions", (k) => /function/.test(k)]];
const iconOf = (k) => (k === "gcp_run_function" ? "fn" : k.startsWith("gcp_") ? "run" : k.startsWith("azure_functions") ? "func" : "aca");

// The saved configuration that targets this cloud workload, if any (same kind, same workload name).
const configOf = (w) => ls.configs.find((c) => c.kind === w.kind && String(c.workload || "").split(" / ").pop() === w.name);

function row(w) {
  const cfg = configOf(w);
  const open = cfg ? href.workload(cfg.name) : `${href.create()}?kind=${encodeURIComponent(w.kind)}`;
  const [tone, label] = DOT[!!cfg];
  const tr = h("tr", { class: "row-link", "data-name": w.name },
    h("td", { class: "col-icon" }, h("span", { class: `ov-dot ov-dot-${tone}`, role: "img", "aria-label": label, title: label })),
    h("td", {}, h("a", { href: open, text: w.name })),
    h("td", { text: w.region || "—" }),
    h("td", { text: ls.schema?.kinds?.[w.kind]?.label || w.kind }),
    h("td", { text: w.project || w.resource_group || "—" }));
  tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.assign(open); });
  return tr;
}

function render() {
  fill($("wl-rows"), ls.rows.map(row));
  $("wl-empty").hidden = !ls.checked || ls.rows.length > 0;
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

// What exists in the cloud now (the same read-only listing the New workload picker uses), and the
// saved configurations, to mark and open the onboarded ones.
async function load(refresh = false) {
  ls.checked = false;
  render();
  setStatus($("list-status"), "Reading the cloud", "busy");
  const [inv, { configs }] = await Promise.all([api(`/api/inventory/${CLOUD}${refresh ? "?refresh=1" : ""}`), api("/api/configs")]);
  ls.configs = configs;
  ls.rows = (inv.workloads || []).filter((w) => w.supported && inSection(w.kind));
  ls.checked = true;
  render();
  const errs = inv.errors || [];
  setStatus($("list-status"), errs.length ? `Could not read part of the cloud: ${errs[0]}` : "", errs.length ? "error" : "");
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
  $("wl-help").append(help("What exists in the cloud now. Green: onboarded to JFrog OIDC here; grey: not onboarded (opens New workload)."));
  $("wl-where").textContent = CLOUD === "azure" ? "Resource group" : "Project";
  fill($("wl-create"), tiles());
  $("wl-refresh").addEventListener("click", () => load(true).catch((e) => setStatus($("list-status"), e.message, "error")));
  try {
    await load();
  } catch (e) {
    setStatus($("list-status"), `Could not list workloads: ${e.message}`, "error");
  }
}
