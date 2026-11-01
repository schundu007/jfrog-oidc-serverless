// Settings (/settings): who the consoles act as. One card per tool, all the same size: what it is signed
// in as, where that login lives (this machine or this console), and the sign-in. Google Cloud shows
// Google's link and takes the code back; Azure and AWS SSO show a device code; JFrog takes the platform
// URL and an admin token, which the server keeps in memory only (never shown again). Azure DevOps names
// the organization and project; it signs in through Azure. API: /api/settings (portal/server.py).
import { api, h, help } from "/serverless/static/core.js";

const LOGO = {"gcp": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 19.3\" aria-hidden=\"true\"><path fill=\"#EA4335\" d=\"M15.2 5.3h.7l2-2 .1-.9C14.2-.9 8.5-.5 5.2 3.3c-.9 1-1.6 2.3-2 3.6.2-.1.5-.1.7 0l4-.7s.2-.3.3-.3c1.8-2 4.8-2.2 6.9-.5h.1z\"/><path fill=\"#4285F4\" d=\"M20.8 6.9c-.5-1.7-1.4-3.2-2.7-4.4l-2.8 2.8c1.2 1 1.9 2.5 1.8 4v.5c1.4 0 2.5 1.1 2.5 2.5s-1.1 2.5-2.5 2.5h-5l-.5.5v3l.5.5h5c3.6 0 6.5-2.9 6.5-6.5 0-2.2-1.1-4.2-2.8-5.4z\"/><path fill=\"#34A853\" d=\"M7.1 19.3h5v-4h-5c-.4 0-.7-.1-1-.2l-.7.2-2 2-.2.7c1.1.8 2.5 1.3 3.9 1.3z\"/><path fill=\"#FBBC05\" d=\"M7.1 6.3C3.5 6.3.6 9.3.6 12.9c0 2 1 3.9 2.6 5.2l2.9-2.9c-1.3-.6-1.8-2.1-1.3-3.3.6-1.3 2.1-1.8 3.3-1.3.6.3 1 .7 1.3 1.3l2.9-2.9c-1.2-1.6-3.1-2.6-5.2-2.6z\"/></svg>","azure": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#0078D4\" d=\"M5.483 21.3H24L14.025 4.013l-3.038 8.347 5.836 6.938L5.483 21.3zM13.23 2.7L6.105 8.677 0 19.253h5.505v.014L13.23 2.7z\"/></svg>","ado": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><path fill=\"#0078D4\" d=\"M17 4v9.74l-4 3.28-6.2-2.26V17l-3.51-4.59 10.23.8V4.44zm-3.41.49L7.85 1v2.29L2.58 4.84 1 6.87v4.61l2.26 1V6.57z\"/></svg>","jfrog": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect width=\"24\" height=\"24\" rx=\"5\" fill=\"#40BE46\"/><circle cx=\"8\" cy=\"9\" r=\"2.2\" fill=\"#fff\"/><circle cx=\"16\" cy=\"9\" r=\"2.2\" fill=\"#fff\"/><path d=\"M6 14.5c1.6 2.2 3.7 3.2 6 3.2s4.4-1 6-3.2\" stroke=\"#fff\" stroke-width=\"2\" fill=\"none\" stroke-linecap=\"round\"/></svg>","aws": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect width=\"24\" height=\"24\" rx=\"5\" fill=\"#232F3E\"/><text x=\"12\" y=\"13\" text-anchor=\"middle\" font-family=\"Arial,Helvetica,sans-serif\" font-weight=\"700\" font-size=\"8\" fill=\"#fff\">aws</text><path d=\"M6 16c3.6 2 8.4 2 12 0\" stroke=\"#FF9900\" stroke-width=\"1.6\" fill=\"none\" stroke-linecap=\"round\"/><path d=\"M16.6 15.2l1.6.6-.5 1.6\" stroke=\"#FF9900\" stroke-width=\"1.4\" fill=\"none\" stroke-linecap=\"round\"/></svg>"};   // constant SVG only
const $ = (id) => document.getElementById(id);
let state = null, timer = null;
const drafts = {};   // unsaved field values, kept across redraws

const logo = (k) => { const s = h("span", { class: "conn-logo" }); s.innerHTML = LOGO[k] || ""; return s; };
const SOURCE = { console: "Signed in here", machine: "This machine's login", "": "None" };

function facts(rows) {
  return h("table", { class: "blade-table conn-facts" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Item"), h("th", { scope: "col" }, "Value"))),
    h("tbody", {}, ...rows.filter(Boolean).map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, v || "—")))));
}

function field(tool, key, label, { type = "text", value = "", placeholder = "", list = null, tip = null } = {}) {
  const id = `f-${tool}-${key}`;
  const cur = drafts[id] ?? value ?? "";
  let input;
  if (list) {
    input = h("select", { id }, h("option", { value: "" }, list.length ? "Choose…" : "None found"),
      ...list.map(([v, t]) => h("option", { value: v, selected: v === cur || null }, t)));
  } else {
    input = h("input", { id, type, value: type === "password" ? "" : cur, placeholder, autocomplete: "off", spellcheck: "false" });
  }
  if (type !== "password") input.addEventListener("input", () => { drafts[id] = input.value; });
  input.addEventListener("change", () => { if (type !== "password") drafts[id] = input.value; });
  return h("label", { class: "field", for: id }, h("span", { class: "field-label" }, label, tip ? help(tip) : null), input);
}
const val = (tool, key) => ($(`f-${tool}-${key}`)?.value || "").trim();

function busy(btn, fn) {
  return async () => {
    btn.disabled = true;
    $("set-error").hidden = true;
    try { await fn(); } catch (e) { showError(e.message); } finally { btn.disabled = false; }
  };
}
function button(text, fn, cls = "secondary") { const b = h("button", { type: "button", class: cls }, text); b.addEventListener("click", busy(b, fn)); return b; }
function showError(msg) { const e = $("set-error"); e.textContent = msg; e.hidden = false; }

const post = (tool, act, body = {}) => api(`/api/settings/${tool}/${act}`, { method: "POST", body, abs: true });
const put = (tool, body) => api(`/api/settings/${tool}`, { method: "PUT", body, abs: true });

// A sign-in in progress: the link to open, the device code or a box for Google's code, and Cancel.
function pending(tool, p) {
  if (!p) return null;
  if (p.state === "done") return null;
  if (p.state === "failed") return h("p", { class: "conn-msg bad" }, `Sign-in failed: ${p.error || "see the CLI output"}`);
  const kids = [];
  if (p.url) kids.push(h("a", { class: "btn primary", href: p.url, target: "_blank", rel: "noopener noreferrer" }, "Open sign-in page"));
  else kids.push(h("span", { class: "muted" }, "Starting…"));
  if (p.code) kids.push(h("span", { class: "conn-code", "aria-label": "Device code" }, p.code));
  const box = h("div", { class: "conn-pending" }, h("div", { class: "conn-row" }, ...kids));
  if (p.needs_code && p.url) {
    const code = field(tool, "code", "Code from the sign-in page", { type: "password", placeholder: "Paste the code" });
    box.append(h("div", { class: "conn-row conn-inline" }, code,
      button("Send", async () => { await post(tool, "code", { code: val(tool, "code") }); await load(); }, "primary")));
  }
  box.append(h("div", { class: "conn-row" }, button("Cancel", async () => { await post(tool, "cancel"); await load(); })));
  return box;
}

function card(tool, title, status, body) {
  const ok = status.signed_in;
  return h("section", { class: "conn-card", "aria-labelledby": `h-${tool}` },
    h("header", { class: "conn-head" }, logo(tool), h("h2", { id: `h-${tool}` }, title),
      h("span", { class: `blade-state ${ok ? "ok" : "warn"}` }, ok ? "Connected" : "Not connected")),
    ...body.filter(Boolean));
}

function gcp(s) {
  const pick = state.choices?.gcp;
  return card("gcp", "Google Cloud", s, [
    facts([["Account", s.account], ["Project", s.project], ["Login", SOURCE[s.source]]]),
    !s.cli ? h("p", { class: "conn-msg bad" }, "gcloud is not installed on this machine.") : null,
    pending("gcp", s.pending),
    pick ? h("div", { class: "conn-row conn-inline" },
      field("gcp", "project", "Project", { list: pick.map((p) => [p.id, p.name === p.id ? p.id : `${p.name} (${p.id})`]), value: s.project }),
      button("Use", async () => { await put("gcp", { project: val("gcp", "project") }); await load(); }, "primary")) : null,
    h("div", { class: "conn-actions" },
      button(s.source === "console" ? "Sign in again" : "Sign in", async () => { await post("gcp", "signin"); await load(); }, "primary"),
      s.signed_in ? button("Change project", async () => { await choices("gcp"); }) : null,
      s.source === "console" ? button("Sign out", async () => { await post("gcp", "signout"); await load(); }) : null),
  ]);
}

function azure(s) {
  const pick = state.choices?.azure;
  return card("azure", "Azure", s, [
    facts([["Account", s.account], ["Subscription", s.subscription], ["Tenant", s.tenant], ["Login", SOURCE[s.source]]]),
    !s.cli ? h("p", { class: "conn-msg bad" }, "Azure CLI is not installed on this machine.") : null,
    pending("azure", s.pending),
    pick ? h("div", { class: "conn-row conn-inline" },
      field("azure", "subscription", "Subscription", { list: pick.map((a) => [a.id, a.name]), value: s.subscription_id }),
      button("Use", async () => { await put("azure", { subscription: val("azure", "subscription") }); await load(); }, "primary")) : null,
    field("azure", "tenant", "Tenant", { value: s.tenant_wanted, placeholder: "Default tenant",
      tip: "Optional. A tenant ID or domain (contoso.onmicrosoft.com) when the account belongs to more than one." }),
    h("div", { class: "conn-actions" },
      button(s.source === "console" ? "Sign in again" : "Sign in", async () => { await post("azure", "signin", { tenant: val("azure", "tenant") }); await load(); }, "primary"),
      s.signed_in ? button("Change subscription", async () => { await choices("azure"); }) : null,
      s.source === "console" ? button("Sign out", async () => { await post("azure", "signout"); await load(); }) : null),
  ]);
}

function ado(s) {
  const v = s.values || {};
  return card("ado", "Azure DevOps", s, [
    facts([["Account", s.account], ["Project", s.project_ok === null ? "" : s.project_ok ? "Found" : "Not found"]]),
    s.error ? h("p", { class: "conn-msg bad" }, s.error) : null,
    h("div", { class: "conn-fields" },
      field("ado", "organization", "Organization", { value: v.organization, placeholder: "contoso" }),
      field("ado", "project", "Project", { value: v.project }),
      field("ado", "repository", "Repository", { value: v.repository }),
      field("ado", "target_branch", "Branch", { value: v.target_branch, placeholder: "main" })),
    h("div", { class: "conn-actions" },
      button("Save", async () => {
        await put("ado", { organization: val("ado", "organization"), project: val("ado", "project"),
          repository: val("ado", "repository"), target_branch: val("ado", "target_branch") });
        for (const k of Object.keys(drafts)) if (k.startsWith("f-ado-")) delete drafts[k];
        await load();
      }, "primary")),
  ]);
}

function jfrog(s) {
  return card("jfrog", "JFrog", s, [
    facts([["Platform", s.url], ["Admin token", s.token_present ? (s.source === "console" ? "Entered here" : "This machine's") : ""],
      ["Admin rights", s.admin === null ? "" : s.admin ? "Yes" : "No"], ["Console operator", s.operator]]),
    s.error ? h("p", { class: "conn-msg bad" }, s.error) : null,
    h("div", { class: "conn-fields" },
      field("jfrog", "url", "Platform URL", { type: "url", value: s.url, placeholder: "https://acme.jfrog.io" }),
      field("jfrog", "token", "Admin access token", { type: "password", placeholder: s.token_present ? "Entered" : "Paste a token",
        tip: "Kept in this console's memory only: never saved, logged or shown. Enter it again after a restart." })),
    h("div", { class: "conn-actions" },
      button("Sign in", async () => {
        await post("jfrog", "signin", { url: val("jfrog", "url"), token: $("f-jfrog-token")?.value || "" });
        delete drafts["f-jfrog-url"];
        await load();
      }, "primary"),
      s.source === "console" ? button("Sign out", async () => { await post("jfrog", "signout"); await load(); }) : null),
  ]);
}

function aws(s) {
  return card("aws", "AWS", s, [
    facts([["Account", s.account], ["Identity", s.arn], ["Profile", s.profile], ["Region", s.region]]),
    !s.cli ? h("p", { class: "conn-msg bad" }, "AWS CLI is not installed on this machine.") : null,
    pending("aws", s.pending),
    h("div", { class: "conn-fields" },
      field("aws", "profile", "Profile", { list: (s.profiles || []).map((p) => [p, p]), value: s.profile }),
      field("aws", "region", "Region", { value: s.region, placeholder: "us-east-1" })),
    h("div", { class: "conn-actions" },
      button("Save", async () => { await put("aws", { profile: val("aws", "profile"), region: val("aws", "region") }); await load(); }, "primary"),
      button("SSO sign-in", async () => { await post("aws", "signin", { profile: val("aws", "profile") }); await load(); })),
  ]);
}

async function choices(tool) {
  const r = await api(`/api/settings/${tool}/choices`, { abs: true });
  state.choices = { ...(state.choices || {}), [tool]: r.items || [] };
  draw();
}

function draw() {
  const root = $("set-cards");
  const keep = document.activeElement?.id;
  root.replaceChildren(gcp(state.gcp), azure(state.azure), ado(state.ado), jfrog(state.jfrog), aws(state.aws));
  root.setAttribute("aria-busy", "false");
  if (keep) $(keep)?.focus();
}

async function load() {
  clearTimeout(timer);
  try {
    const s = await api("/api/settings", { abs: true });
    state = { ...s, choices: state?.choices };
    draw();
    const waiting = ["gcp", "azure", "aws"].some((t) => s[t]?.pending?.state === "waiting");
    if (waiting) timer = setTimeout(load, 2500);
  } catch (e) {
    showError(e.status === 401 ? "Signed out of the console: open it again from the link the portal printed." : e.message);
  }
}

$("set-refresh").addEventListener("click", () => load());
load();
