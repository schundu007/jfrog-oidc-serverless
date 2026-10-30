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
  fill(P,
    pageHead("Install", tmpl ? "Cluster template pipeline (jfrog-kcp-cluster)" : "Utility pipeline scenarios. DryRun first; every change waits for your approval."),
    list,
    h("details", { class: "advanced" }, h("summary", {}, "All pipeline scenarios"),
      table(["scenario", "does", "changes state", ""], all.map((s) => [h("code", {}, s.name), s.does,
        s.changes_state + (s.restarts_daemonset ? " · restarts the DaemonSet" : ""),
        runControl({ kind: "pilot", name: s.name, dryRun: scenarioDry(s.name), label: "Prepare" })]))));
}
