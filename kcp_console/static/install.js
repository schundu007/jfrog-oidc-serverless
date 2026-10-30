// Install: the set-up as ordered actions, each with its newest run and a run control.

import { h, fill, state } from "./app.js";
import { $, table, runControl, needConfig, stepList, pageHead, scenarioDry } from "./ui.js";

const PILOT = [
  ["Check the nodes", "Reads each node's provider config and kubelet start time (read-only).", "pilot", "CHECK_KUBELET_PROVIDER"],
  ["Mirror the plugin binary", "Copies jfrog-credential-provider into your generic repo for downloadUrl.", "pilot", "MIRROR_JFROG_BINARY"],
  ["Create the OIDC provider", "JFrog trusts the cluster's OIDC issuer (provider_type Azure).", "jfrog", "create-provider"],
  ["Create the identity mapping", "system:serviceaccount:<ns>:<sa> → the mapped user's short-lived token.", "jfrog", "create-mapping"],
  ["Put a test image in the repo", "Promote an existing image into the scoped Docker repo.", "jfrog", "promote"],
  ["Install the credential provider", "Helm install through the utility pipeline; labels the nodes.", "pilot", "INSTALL_JFROG_PROVIDER"],
  ["Check the provider pods", "Rendered config and DaemonSet pod logs.", "pilot", "CHECK_JFROG_POD_CONFIG"],
  ["Pull test", "A pod with imagePullSecrets: [] pulls the test image.", "pilot", "CREATE_TEST_POD"],
];

const DIRECT = [
  ["Set up", "Cluster OIDC issuer, service account, JFrog provider and mapping, node label, helm install, DaemonSet, pull test.", "setup"],
  ["Verify", "DaemonSet ready, then a pod with imagePullSecrets: [] pulls the test image.", "verify"],
];

export function initInstall() {
  state.on("config", render);
  state.on("runs", render);
}

async function render() {
  const P = $("panel-install");
  if (needConfig(P)) return;
  const k = state.knowledge;
  const cfg = state.configs.find((c) => c.name === state.config);
  const tmpl = cfg && cfg.mode === "template";
  const all = k.PILOT_SCENARIOS.concat(k.CONSOLE_SCENARIOS);
  const list = tmpl
    ? await stepList(k.TEMPLATE_ACTIONS.map((a) => ({ title: a.name, line: a.does, kind: "template", name: a.name, dryRun: true }))
      .concat([{ title: "Identity mappings", line: "jfrog-kcp-mappings: apply the mappings file (Prune stays false).", kind: "mappings", name: "Apply", dryRun: true, params: { Prune: false } }]))
    : await stepList(PILOT.map(([title, line, kind, name]) => ({ title, line, kind, name })));
  const directList = tmpl ? null : await stepList(DIRECT.map(([title, line, name]) => ({ title, line, kind: "direct", name, dryRun: true,
    label: "Prepare (shows every command)" })));
  fill(P,
    pageHead("Install", tmpl ? "Cluster template pipeline (jfrog-kcp-cluster)" : "AZURE.md Option B. DryRun first; every change waits for your approval."),
    directList ? h("section", { class: "card" }, h("h2", {}, "Run directly"),
      h("p", { class: "small muted" }, "With your az login and JFROG_ACCESS_TOKEN: az, kubectl, helm and the JFrog Access API, step by step. Steps already in place are left alone."),
      directList) : null,
    directList ? h("h2", {}, "Or through the utility pipeline") : null,
    list,
    h("details", { class: "advanced" }, h("summary", {}, "All pipeline scenarios"),
      table(["scenario", "does", "changes state", ""], all.map((s) => [h("code", {}, s.name), s.does,
        s.changes_state + (s.restarts_daemonset ? " · restarts the DaemonSet" : ""),
        runControl({ kind: "pilot", name: s.name, dryRun: scenarioDry(s.name), label: "Prepare" })]))));
}
