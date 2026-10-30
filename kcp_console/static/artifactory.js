// Artifactory: read checks with their results, the OIDC providers and mappings, and the writes
// (each a run behind approval). Protected providers are never offered for change.

import { api, h, fill, state } from "./app.js";
import { $, table, pill, runControl, needConfig, pageHead } from "./ui.js";

export function initArtifactory() {
  state.on("config", render);
  state.on("status", render);
}

const call = (action, body = {}) => api(`/api/artifactory/${action}`, { method: "POST", body: { config: state.config, ...body } });

function check(title, line, action, show, extra) {
  const out = h("div", { class: "tool-out" });
  const btn = h("button", { class: "secondary", onclick: async () => {
    fill(out, h("p", { class: "muted small" }, "…"));
    try { fill(out, show(await call(action, extra ? extra() : {}))); } catch (e) { fill(out, h("p", { class: "error small" }, e.message)); }
  } }, "Run");
  return h("div", { class: "check-card" }, h("div", { class: "act-head" }, h("div", { class: "act-text" }, h("b", {}, title), h("span", { class: "small muted" }, line)), btn), out);
}

async function render() {
  const P = $("panel-artifactory");
  if (needConfig(P)) return;
  const prefix = h("input", { type: "text", value: "", placeholder: "path prefix, e.g. team/app", "aria-label": "AQL path prefix", spellcheck: "false" });
  let m = null;
  try { m = await call("mappings"); } catch { /* not discovered */ }
  fill(P,
    pageHead("Artifactory", "Reads use JFROG_ACCESS_TOKEN from the console's environment (never shown). Writes wait for your approval."),
    h("div", { class: "check-grid" },
      check("Registry host", "GET /v2/ on the repo subdomain and the bare domain: does the realm name the repo?", "probe",
        (r) => table(["host", "status", "verdict"], r.probes.map((p) => [p.host, String(p.status), h("span", {}, pill(p.check.status), " ", p.check.detail)]))),
      check("Find the image", "AQL search across repos.", "aql",
        (r) => h("div", {}, table(["repo", "path", "name"], r.results.map((x) => [x.repo, x.path, x.name])),
          h("p", { class: "small" }, "Local repos: ", r.local.join(", ") || "none")), () => ({ prefix: prefix.value })),
      check("Test image in repo", "Storage API on the scoped repo.", "storage", (r) => h("pre", { class: "code" }, JSON.stringify(r.json, null, 2))),
      check("Plugin binary", "Mirrored size and sha256.", "binary",
        (r) => h("p", {}, pill(r.check.status), " ", r.check.detail)),
      check("Token scope", "Read-only GET /access/api/v2/groups.", "groups", (r) => h("p", {}, pill(r.check.status), " HTTP ", String(r.status), ". ", r.check.detail)),
      check("Remote repository", "Is the remote dormant?", "remote", (r) => h("p", {}, pill(r.check.status), " ", r.check.detail))),
    h("div", { class: "toolbar" }, h("span", { class: "small muted" }, "AQL prefix"), prefix),
    h("h2", {}, "OIDC providers"),
    m ? table(["name", "issuer", "type", ""], m.providers.map((p) => [h("b", {}, p.name), p.issuer_url, p.provider_type || "",
      m.protected.includes(p.name) ? h("span", { class: "tag" }, "protected") : p.name === m.provider ? pill("this cluster") : ""]))
      : h("p", { class: "muted" }, "Run Discover on Configure to read them."),
    m && !m.providers.some((p) => p.name === m.provider) ? runControl({ kind: "jfrog", name: "create-provider", label: `Create ${m.provider}` }) : null,
    h("h2", {}, "Identity mappings"),
    m && m.mappings.length ? table(["name", "sub", "user", "scope", "expires", "subject", ""], m.mappings.map((x) => {
      const o = m.orphaned.find((y) => y.mapping === x.name) || {};
      return [h("b", {}, x.name), x.claims.sub, x.token_spec.username, x.token_spec.scope, `${x.token_spec.expires_in}s`, pill(o.status || "unknown"),
        runControl({ kind: "jfrog", name: "delete-mapping", params: { mapping: x.name }, label: "Delete" })];
    })) : h("p", { class: "muted" }, "None yet."),
    h("div", { class: "toolbar" },
      runControl({ kind: "jfrog", name: "create-mapping", label: "Create mapping from the config" }),
      runControl({ kind: "jfrog", name: "promote", label: "Promote test image" })),
    m ? h("details", { class: "advanced" }, h("summary", {}, "Request previews"),
      h("div", { class: "two-col" }, h("pre", { class: "code" }, JSON.stringify(m.provider_preview, null, 2)), h("pre", { class: "code" }, JSON.stringify(m.preview, null, 2)))) : null);
}
