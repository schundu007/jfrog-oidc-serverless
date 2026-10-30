// Nodes: each node's provider entry against the release, kubelet restart state, writers, and
// the node actions.

import { api, h, fill, state } from "./app.js";
import { $, table, pill, layersView, needConfig, stepList, pageHead } from "./ui.js";

export function initNodes() {
  state.on("config", render);
  state.on("runs", render);
  state.on("status", render);
}

async function render() {
  const P = $("panel-nodes");
  if (needConfig(P)) return;
  let a;
  try { a = await api(`/api/nodes?config=${encodeURIComponent(state.config)}`); } catch (e) { fill(P, h("p", { class: "error" }, e.message)); return; }
  const nodes = (a.three_layers && a.checks_count) ? a.three_layers.nodes : [];
  const restart = Object.fromEntries(a.kubelet_restart.map((r) => [r.node, r]));
  fill(P,
    pageHead("Nodes"),
    nodes.length ? table(["node", "provider entry", "problems", "kubelet", "new"], nodes.map((n) => [
      h("b", {}, n.node || "—"), pill(n.status),
      n.problems.length ? n.problems.map((p) => `${p.layer} ${p.field}`).join(", ") : "none",
      restart[n.node] ? pill(restart[n.node].status === "pass" ? "restarted" : restart[n.node].status === "fail" ? "stale" : "unknown") : "—",
      a.fresh_nodes.includes(n.node) ? "yes" : ""]))
      : h("p", { class: "muted" }, "No node data yet: run Check the nodes."),
    nodes.length ? h("details", { class: "advanced" }, h("summary", {}, "Per-node detail (values, release, node file, plugin URL)"), layersView(a.three_layers)) : null,
    h("h2", {}, "Writers of the node config"),
    a.writers.length ? table(["DaemonSet", "mounts", "role"], a.writers.map((d) => [`${d.namespace}/${d.name}`, d.mounts.join(", "),
      d.release ? "the release" : d.jfrog ? h("b", { class: "error" }, "orphaned JFrog writer") : "other"]))
      : h("p", { class: "muted small" }, "Not read yet: run Discover or Find writers."),
    h("h2", {}, "Actions"),
    await stepList([
      { title: "Check the nodes", line: "CHECK_KUBELET_PROVIDER (read-only).", kind: "pilot", name: "CHECK_KUBELET_PROVIDER" },
      { title: "Find writers", line: "Every DaemonSet that mounts the kubelet config path.", kind: "pilot", name: "FIND_CREDENTIAL_PROVIDER_WRITERS" },
      { title: "Reset a node's JFrog entry", line: "Rewrites a stale entry from the release (backup kept).", kind: "pilot", name: "RESET_NODE_JFROG_ENTRY" },
      { title: "Remove an orphaned DaemonSet", line: "Deletes a JFrog DaemonSet that is not the release's.", kind: "pilot", name: "CLEANUP_STALE_JFROG_DAEMONSET" },
    ]));
}
