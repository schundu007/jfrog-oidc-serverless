// Overview: the saved clusters with their Preflight and last pull, the selected cluster's state
// and next step, and how the exchange works.

import { api, h, fill, state, selectTab, refreshConfigs } from "./app.js";
import { beforeAfter, optionBFlow, nodeDiagram, pipeline } from "./diagrams.js";
import { pill } from "./ui.js";

const $ = (id) => document.getElementById(id);

export function initOverview(k) {
  fill($("dg-before-after"), beforeAfter(k.DIAGRAMS.before_after));
  fill($("dg-flow"), optionBFlow(k.DIAGRAMS.option_b_flow));
  $("dg-flow-note").textContent = `${k.DIAGRAMS.option_b_flow.note} ${k.DIAGRAMS.option_b_flow.why_b}`;
  fill($("dg-node"), nodeDiagram(k.DIAGRAMS.node));
  $("dg-node-note").textContent = k.DIAGRAMS.node.kubelet;
  fill($("dg-pipeline"), pipeline(k.PIPELINE_DIAGRAM));
  $("ov-new").addEventListener("click", () => state.emit("new-config"));
  state.on("config", render);
  state.on("runs", render);
  state.on("status", render);
  render();
}

async function render() {
  const configs = state.configs || [];
  let runs = [];
  try { runs = (await api("/api/runs")).runs; } catch { /* none */ }
  const statuses = await Promise.all(configs.map((c) => api(`/api/status/${encodeURIComponent(c.name)}`).then((r) => r.status).catch(() => ({}))));
  const lastPull = (name) => runs.find((r) => r.config === name && /CREATE_(TEST|CONTROL)_POD/.test(r.name));
  fill($("ov-clusters"), configs.length ? configs.map((c, i) => {
    const pf = statuses[i].preflight, pull = lastPull(c.name);
    return h("tr", { class: c.name === state.config ? "is-open" : "" },
      h("td", {}, h("button", { class: "link cfg-open", onclick: () => open(c.name, "configure") }, c.name)),
      h("td", {}, c.cluster || h("span", { class: "muted" }, "not set")),
      h("td", {}, c.mode === "template" ? "cluster template" : "utility scenarios"),
      h("td", {}, pf ? pill(pf.status) : h("span", { class: "muted small" }, "not run")),
      h("td", {}, pull ? h("span", {}, pill(pull.status), " ", h("span", { class: "small muted" }, pull.created.slice(0, 10))) : h("span", { class: "muted small" }, "none")),
      h("td", { class: "small muted" }, (c.updated || "").slice(0, 16).replace("T", " ")),
      h("td", {}, h("button", { class: "secondary small-btn", onclick: () => open(c.name, "overview") }, c.name === state.config ? "Selected" : "Select")));
  }) : h("tr", {}, h("td", { colspan: "7", class: "muted" }, "No clusters yet. Click New cluster.")));
  const cur = configs.findIndex((c) => c.name === state.config);
  fill($("ov-current"), cur < 0 ? null : current(configs[cur], statuses[cur], runs.filter((r) => r.config === state.config)));
}

async function open(name, tab) {
  await refreshConfigs(name);
  state.emit("config", name);
  if (tab !== "overview") selectTab(tab);
}

// The selected cluster: where it stands and the one next step.
function current(c, st, runs) {
  const pf = st.preflight;
  const failed = (pf?.checks || []).filter((x) => x.status === "fail");
  const pull = runs.find((r) => /CREATE_(TEST|CONTROL)_POD/.test(r.name));
  const installed = runs.some((r) => r.name === "INSTALL_JFROG_PROVIDER" && r.status === "succeeded" && r.request.parameters.DryRun !== true);
  let next;
  if (!pf) next = ["Run Preflight", "configure", "Checks the issuer, Workload Identity, the JFrog provider and mapping, the service account and the release."];
  else if (failed.length) next = [`Fix: ${failed[0].title}`, "configure", failed[0].fix || failed[0].detail];
  else if (!installed) next = ["Install the provider", "install", "Mirror the binary, create the provider and mapping, then INSTALL_JFROG_PROVIDER (DryRun first)."];
  else if (!pull || pull.status !== "succeeded") next = ["Run the pull test", "verify", "CREATE_TEST_POD pulls through the plugin with imagePullSecrets: []."];
  else next = ["Check the node config", "nodes", "CHECK_KUBELET_PROVIDER confirms each node's provider entry against the release."];
  const stat = (label, value) => h("div", { class: "stat" }, h("span", { class: "small muted" }, label), h("b", {}, value));
  return h("div", { class: "summary current" },
    h("h2", {}, c.name, h("span", { class: "muted" }, ` · ${c.cluster || "cluster not set"}`)),
    h("div", { class: "stats" },
      stat("Preflight", pf ? pill(pf.status) : "not run"),
      stat("Failing checks", String(failed.length)),
      stat("Provider installed", installed ? "yes" : "no"),
      stat("Last pull test", pull ? pill(pull.status) : "none"),
      stat("Runs", String(runs.length))),
    h("div", { class: "outcome" }, h("div", {}, h("b", {}, "Next: "), next[0], h("p", { class: "small muted" }, next[2])),
      h("button", { class: "primary", onclick: () => selectTab(next[1]) }, "Go")));
}
