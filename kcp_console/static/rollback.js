// Rollback: each undo as a run (destructive ones need the cluster name typed in). Protected
// providers are never offered.

import { h, fill, state } from "./app.js";
import { $, needConfig, stepList, pageHead } from "./ui.js";

export function initRollback() {
  state.on("config", render);
  state.on("runs", render);
}

async function render() {
  const P = $("panel-rollback");
  if (needConfig(P)) return;
  const k = state.knowledge;
  const items = k.ROLLBACK.flatMap((r) => r.jfrog_write
    ? [{ title: "Delete the identity mapping", line: "DELETE …/identity_mappings/<name>", kind: "jfrog", name: "delete-mapping" },
      { title: "Delete the OIDC provider", line: "DELETE /access/api/v1/oidc/<name> (protected providers refused)", kind: "jfrog", name: "delete-provider" }]
    : [{ title: r.title, line: r.command, kind: "pilot", name: r.scenario, params: r.params || {} }]);
  const tmpl = (state.configs.find((c) => c.name === state.config) || {}).mode === "template";
  const teardown = tmpl ? null : await stepList([{ title: "Tear down (direct)", kind: "direct", name: "teardown", dryRun: true,
    line: "helm uninstall, remove the node label, delete the identity mapping and the OIDC provider (protected providers refused).",
    label: "Run" }]);
  fill(P, pageHead("Rollback"), teardown,
    teardown ? h("h2", {}, "Pipeline") : null, await stepList(items));
}
