// Verify: the newest pull's verdict, the pull tests, the control pod and audit attribution.

import { api, h, fill, state } from "./app.js";
import { $, table, pill, needConfig, stepList, pageHead } from "./ui.js";

export function initVerify() {
  state.on("config", render);
  state.on("runs", render);
}

async function render() {
  const P = $("panel-verify");
  if (needConfig(P)) return;
  let a;
  try { a = await api(`/api/nodes?config=${encodeURIComponent(state.config)}`); } catch (e) { fill(P, h("p", { class: "error" }, e.message)); return; }
  const v = a.verdict, proof = v.proof;
  const out = h("div", {});
  const paste = h("textarea", { rows: "3", class: "paste", placeholder: "Optional: paste artifactory-request.log lines", spellcheck: "false" });
  const stat = (label, value) => h("div", { class: "stat" }, h("span", { class: "small muted" }, label), h("b", {}, value));
  fill(P,
    pageHead("Verify", "Does a pod pull through the plugin, with its own identity?"),
    h("div", { class: "summary current" },
      h("div", { class: "stats" },
        stat("Newest pull", pill(v.status)),
        stat("Proof", proof ? pill("pass") : "none yet"),
        stat("Control pod", v.control ? pill(v.control.refused ? "pass" : "fail") : "not run"),
        stat("Audit attribution", pill(v.audit.status)),
        stat("Token cache", a.cache.duration)),
      h("div", { class: "outcome" }, h("div", {}, v.pod ? `${v.pod.status}: ${v.pod.detail || ""}` : "No pull test yet.",
        proof ? h("p", { class: "small muted" }, `${proof.evidence} on ${proof.node}.`) : null))),
    await stepList([
      { title: "Pull test", line: "A pod on the provider's service account pulls the test image.", kind: "pilot", name: "CREATE_TEST_POD" },
      { title: "Control pod", line: "The same image on the default service account must be refused.", kind: "pilot", name: "CREATE_CONTROL_POD" },
      { title: "Audit attribution", line: "The pull must be logged as the mapped user, not a static token.",
        body: h("div", {}, h("div", { class: "toolbar" },
          h("button", { class: "secondary", onclick: () => audit(out) }, "Query the request log"),
          h("button", { class: "secondary", onclick: () => audit(out, paste.value) }, "Check pasted lines")), paste, out) },
    ]),
    h("h2", {}, "Pulls"),
    a.pods.length ? table(["time", "pod", "service account", "node", "verdict", "evidence"],
      a.pods.map((p) => [p.time, `${p.pod}${p.kind === "control" ? " (control)" : ""}`, p.sa, p.node, pill(p.status), p.evidence]))
      : h("p", { class: "muted" }, "No pulls yet."));
}

async function audit(out, text) {
  try {
    const r = await api("/api/verify/audit", { method: "POST", body: { config: state.config, text: text || undefined } });
    fill(out, h("p", {}, pill(r.status), " ", r.detail || "", r.window && r.window.from ? ` (${r.window.from} → ${r.window.to})` : ""));
  } catch (e) { fill(out, h("p", { class: "error small" }, e.message)); }
}
