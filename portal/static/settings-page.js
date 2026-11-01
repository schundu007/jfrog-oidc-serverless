// Settings (/settings): who the consoles act as. One list for every connection (cloud sign-ins, Azure
// DevOps, JFrog, more to come), like an integrations page: stat cards, a filter bar, rows grouped by
// category; a row opens the side panel with its details and sign-in. A new connection is one entry in
// CONNECTORS (its row facts and its panel). Google Cloud shows Google's link and takes the code back;
// Azure and AWS SSO show a device code; the JFrog token is kept in server memory only and never shown.
// API: /api/settings (portal/server.py, portal/connections.py).
import { api, h, help } from "/serverless/static/core.js";

const LOGO = {"gcp": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 19.3\" aria-hidden=\"true\"><path fill=\"#EA4335\" d=\"M15.2 5.3h.7l2-2 .1-.9C14.2-.9 8.5-.5 5.2 3.3c-.9 1-1.6 2.3-2 3.6.2-.1.5-.1.7 0l4-.7s.2-.3.3-.3c1.8-2 4.8-2.2 6.9-.5h.1z\"/><path fill=\"#4285F4\" d=\"M20.8 6.9c-.5-1.7-1.4-3.2-2.7-4.4l-2.8 2.8c1.2 1 1.9 2.5 1.8 4v.5c1.4 0 2.5 1.1 2.5 2.5s-1.1 2.5-2.5 2.5h-5l-.5.5v3l.5.5h5c3.6 0 6.5-2.9 6.5-6.5 0-2.2-1.1-4.2-2.8-5.4z\"/><path fill=\"#34A853\" d=\"M7.1 19.3h5v-4h-5c-.4 0-.7-.1-1-.2l-.7.2-2 2-.2.7c1.1.8 2.5 1.3 3.9 1.3z\"/><path fill=\"#FBBC05\" d=\"M7.1 6.3C3.5 6.3.6 9.3.6 12.9c0 2 1 3.9 2.6 5.2l2.9-2.9c-1.3-.6-1.8-2.1-1.3-3.3.6-1.3 2.1-1.8 3.3-1.3.6.3 1 .7 1.3 1.3l2.9-2.9c-1.2-1.6-3.1-2.6-5.2-2.6z\"/></svg>","azure": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#0078D4\" d=\"M5.483 21.3H24L14.025 4.013l-3.038 8.347 5.836 6.938L5.483 21.3zM13.23 2.7L6.105 8.677 0 19.253h5.505v.014L13.23 2.7z\"/></svg>","ado": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><path fill=\"#0078D4\" d=\"M17 4v9.74l-4 3.28-6.2-2.26V17l-3.51-4.59 10.23.8V4.44zm-3.41.49L7.85 1v2.29L2.58 4.84 1 6.87v4.61l2.26 1V6.57z\"/></svg>","jfrog": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect width=\"24\" height=\"24\" rx=\"5\" fill=\"#40BE46\"/><circle cx=\"8\" cy=\"9\" r=\"2.2\" fill=\"#fff\"/><circle cx=\"16\" cy=\"9\" r=\"2.2\" fill=\"#fff\"/><path d=\"M6 14.5c1.6 2.2 3.7 3.2 6 3.2s4.4-1 6-3.2\" stroke=\"#fff\" stroke-width=\"2\" fill=\"none\" stroke-linecap=\"round\"/></svg>","aws": "<svg class=\"mark cloud-logo\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect width=\"24\" height=\"24\" rx=\"5\" fill=\"#232F3E\"/><text x=\"12\" y=\"13\" text-anchor=\"middle\" font-family=\"Arial,Helvetica,sans-serif\" font-weight=\"700\" font-size=\"8\" fill=\"#fff\">aws</text><path d=\"M6 16c3.6 2 8.4 2 12 0\" stroke=\"#FF9900\" stroke-width=\"1.6\" fill=\"none\" stroke-linecap=\"round\"/><path d=\"M16.6 15.2l1.6.6-.5 1.6\" stroke=\"#FF9900\" stroke-width=\"1.4\" fill=\"none\" stroke-linecap=\"round\"/></svg>"};   // constant SVG only   // constant SVG only
const $ = (id) => document.getElementById(id);
const SOURCE = { console: "This console", machine: "This machine" };
const CATEGORY = { cloud: "Cloud providers", cicd: "CI/CD", artifacts: "Artifact platforms" };

let state = null, timer = null, open = null;
const drafts = {};   // unsaved field values, kept across redraws
const u0 = new URLSearchParams(location.search);
const F = { q: u0.get("q") || "", category: u0.get("category") || "", status: u0.get("status") || "" };
function saveQuery() {
  const u = new URLSearchParams();
  for (const k of ["q", "category", "status"]) if (F[k]) u.set(k, F[k]);
  if (open) u.set("open", open);
  history.replaceState(null, "", location.pathname + (u.toString() ? `?${u}` : ""));
}

// ------------------------------------------------------------------------------------------- helpers

const logo = (k) => { const s = h("span", { class: "conn-logo" }); s.innerHTML = LOGO[k] || ""; return s; };
const post = (tool, act, body = {}) => api(`/api/settings/${tool}/${act}`, { method: "POST", body, abs: true });
const put = (tool, body) => api(`/api/settings/${tool}`, { method: "PUT", body, abs: true });
const val = (tool, key) => ($(`f-${tool}-${key}`)?.value || "").trim();
function showError(msg) { const e = $("set-error"); e.textContent = msg; e.hidden = !msg; }

function facts(rows) {
  return h("table", { class: "blade-table conn-facts" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Item"), h("th", { scope: "col" }, "Value"))),
    h("tbody", {}, ...rows.filter(Boolean).map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, v || "—")))));
}

function field(tool, key, label, { type = "text", value = "", placeholder = "", list = null, tip = null } = {}) {
  const id = `f-${tool}-${key}`;
  const cur = drafts[id] ?? value ?? "";
  const input = list
    ? h("select", { id }, h("option", { value: "" }, list.length ? "Choose…" : "None found"),
      ...list.map(([v, t]) => h("option", { value: v, selected: v === cur || null }, t)))
    : h("input", { id, type, value: type === "password" ? "" : cur, placeholder, autocomplete: "off", spellcheck: "false" });
  for (const ev of ["input", "change"]) input.addEventListener(ev, () => { if (type !== "password") drafts[id] = input.value; gate(); });
  return h("label", { class: "field", for: id }, h("span", { class: "field-label" }, label, tip ? help(tip) : null), input);
}

// when(): the action can run now (inputs filled and changed, nothing in progress); otherwise greyed out,
// with the reason as its tooltip. Every input re-checks (gate); a running action stays greyed until done.
let busy = false;
function button(text, fn, cls = "secondary", when = null) {
  const b = h("button", { type: "button", class: cls }, text);
  b._when = when;
  b.addEventListener("click", async () => {
    if (b.disabled) return;
    busy = true; gate(); showError("");
    try { await fn(); } catch (e) { showError(e.message); } finally { busy = false; gate(); }
  });
  return b;
}
function gate() {
  for (const b of document.querySelectorAll("#flyout-body button")) {
    const why = busy ? "Working…" : b._when ? b._when() : "";
    b.disabled = !!why;
    if (why) b.title = why; else b.removeAttribute("title");
  }
}
const need = (...checks) => () => checks.map((c) => c()).find(Boolean) || "";
const signingIn = (s) => () => (s.pending?.state === "waiting" ? "A sign-in is in progress" : "");
const changed = (pairs) => () => (pairs.every(([id, cur]) => (val(...id) || "") === (cur || "")) ? "Nothing changed" : "");
const filled = (...ids) => () => (ids.some((id) => !val(...id)) ? "Fill in every field" : "");
const section = (title, ...kids) => h("section", { class: "conn-sec" }, h("h3", {}, title), ...kids.filter(Boolean));
const actions = (...b) => h("div", { class: "conn-actions" }, ...b.filter(Boolean));
const missing = (s, cli) => (s.cli === false ? h("p", { class: "conn-msg bad" }, `${cli} is not installed on this machine.`) : null);

// A sign-in in progress: the link to open, the device code or a box for Google's code, and Cancel.
function pending(tool, p) {
  if (!p || p.state === "done") return null;
  if (p.state === "failed") return h("p", { class: "conn-msg bad" }, `Sign-in failed: ${p.error || "see the output below"}`);
  const box = h("div", { class: "conn-pending" },
    h("div", { class: "conn-step" }, h("b", {}, "1"),
      p.url ? h("a", { class: "btn primary", href: p.url, target: "_blank", rel: "noopener noreferrer" }, "Open sign-in page")
        : h("span", { class: "muted" }, "Starting…")),
    p.code ? h("div", { class: "conn-step" }, h("b", {}, "2"), h("span", {}, "Enter this code"),
      h("span", { class: "conn-code", "aria-label": "Device code" }, p.code)) : null);
  if (p.needs_code && p.url) {
    box.append(h("div", { class: "conn-step conn-inline" }, h("b", {}, "2"),
      field(tool, "code", "Code from the sign-in page", { type: "password", placeholder: "Paste the code" }),
      button("Send", async () => { await post(tool, "code", { code: val(tool, "code") }); await load(); }, "primary",
        () => (val(tool, "code") ? "" : "Paste the code first"))));
  }
  box.append(actions(button("Cancel sign-in", async () => { await post(tool, "cancel"); await load(); })));
  return box;
}
// Which login a tool acts as: this machine's or the one signed in on this console. Both are kept, so
// switching is instant; the console's option needs a sign-in here first.
function loginSwitch(tool, s) {
  const has = s.logins || {};
  const opt = (login, label, why) => {
    const on = s.source === login;
    const b = button(label, async () => { await post(tool, "use", { login }); await load(); }, "", need(signingIn(s),
      () => (on ? "In use" : ""), () => (has[login] ? "" : why)));
    b.setAttribute("aria-pressed", String(on));
    return b;
  };
  return h("div", { class: "field" }, h("span", { class: "field-label" }, "Use login"),
    h("div", { class: "conn-switch", role: "group", "aria-label": "Use login" },
      opt("machine", "This machine", "No login on this machine"), opt("console", "This console", "Sign in on this console first")));
}
// Sign in and sign out take turns: a console login exists → Sign out (removes it); none → Sign in.
// While a sign-in waits, the pending box carries Cancel instead.
function signInOut(tool, s, signIn, when) {
  if (s.pending?.state === "waiting") return null;
  return s.logins?.console
    ? actions(button("Sign out", async () => { await post(tool, "signout"); await load(); }, "secondary"))
    : actions(button("Sign in", async () => { await signIn(); await load(); }, "primary", when));
}

async function choices(tool) {
  const r = await api(`/api/settings/${tool}/choices`, { abs: true });
  state.choices = { ...(state.choices || {}), [tool]: r.items || [] };
  draw();
}

// ------------------------------------------------------------------------------------------- connectors

// status(s): [dot, text] for the list; scope(s): what it points at; panel(s): the side panel's sections.
const CONNECTORS = [
  { id: "gcp", name: "Google Cloud", category: "cloud",
    about: "gcloud and Terraform act as this account. Sign in here to use a login kept by this console instead of this machine's.",
    scope: (s) => s.project,
    panel: (s) => {
      const pick = state.choices?.gcp;
      return [
        section("Connection", facts([["Account", s.account], ["Project", s.project]]), missing(s, "gcloud"), loginSwitch("gcp", s),
          signInOut("gcp", s, () => post("gcp", "signin"), () => (s.cli === false ? "gcloud is not installed" : ""))),
        pending("gcp", s.pending),
        s.signed_in ? section("Project", pick
          ? h("div", { class: "conn-inline" },
            field("gcp", "project", "Project", { list: pick.map((p) => [p.id, p.name === p.id ? p.id : `${p.name} (${p.id})`]), value: s.project }),
            button("Use", async () => { await put("gcp", { project: val("gcp", "project") }); await load(); }, "primary",
              need(signingIn(s), filled(["gcp", "project"]), changed([[["gcp", "project"], s.project]]))))
          : actions(button("Change project", () => choices("gcp"), "secondary", signingIn(s)))) : null,
      ];
    } },
  { id: "azure", name: "Azure", category: "cloud",
    about: "az, Terraform and Azure DevOps act as this account.",
    scope: (s) => s.subscription,
    panel: (s) => {
      const pick = state.choices?.azure;
      return [
        section("Connection", facts([["Account", s.account], ["Subscription", s.subscription], ["Tenant", s.tenant]]),
          missing(s, "Azure CLI"), loginSwitch("azure", s),
          s.logins?.console || s.pending?.state === "waiting" ? null
            : field("azure", "tenant", "Tenant", { value: s.tenant_wanted, placeholder: "Default tenant",
              tip: "Optional. A tenant ID or domain (contoso.onmicrosoft.com) when the account belongs to more than one." }),
          signInOut("azure", s, () => post("azure", "signin", { tenant: val("azure", "tenant") }),
            () => (s.cli === false ? "Azure CLI is not installed" : ""))),
        pending("azure", s.pending),
        s.signed_in ? section("Subscription", pick
          ? h("div", { class: "conn-inline" },
            field("azure", "subscription", "Subscription", { list: pick.map((a) => [a.id, a.name]), value: s.subscription_id }),
            button("Use", async () => { await put("azure", { subscription: val("azure", "subscription") }); await load(); }, "primary",
              need(signingIn(s), filled(["azure", "subscription"]), changed([[["azure", "subscription"], s.subscription_id]]))))
          : actions(button("Change subscription", () => choices("azure"), "secondary", signingIn(s)))) : null,
      ];
    } },
  { id: "aws", name: "AWS", category: "cloud",
    about: "The AWS CLI acts as this profile.",
    scope: (s) => [s.profile, s.region].filter(Boolean).join(" · "),
    panel: (s) => [
      section("Connection", facts([["Account", s.account], ["Identity", s.arn], ["Profile", s.profile], ["Region", s.region]]), missing(s, "AWS CLI")),
      pending("aws", s.pending),
      section("Profile",
        h("div", { class: "conn-fields" },
          field("aws", "profile", "Profile", { list: (s.profiles || []).map((p) => [p, p]), value: s.profile }),
          field("aws", "region", "Region", { value: s.region, placeholder: "us-east-1" })),
        actions(button("Save", async () => { await put("aws", { profile: val("aws", "profile"), region: val("aws", "region") }); await load(); }, "primary",
          need(signingIn(s), filled(["aws", "profile"]), changed([[["aws", "profile"], s.profile], [["aws", "region"], s.region]]),
            () => (/^[a-z]{2}(-[a-z]+)+-\d$/.test(val("aws", "region")) || !val("aws", "region") ? "" : "Region looks like us-east-1"))),
          button("SSO sign-in", async () => { await post("aws", "signin", { profile: val("aws", "profile") }); await load(); }, "secondary",
            need(signingIn(s), () => (s.cli === false ? "AWS CLI is not installed" : ""), () => (val("aws", "profile") ? "" : "Choose a profile first"))))),
    ] },
  { id: "ado", name: "Azure DevOps", category: "cicd",
    about: "Pipelines, pull requests and approvals. Signs in through Azure: the Azure account must be a member of the organization.",
    scope: (s) => [s.values?.organization, s.values?.project].filter(Boolean).join(" / "),
    panel: (s) => {
      const v = s.values || {};
      return [
        section("Connection", facts([["Account", s.account], ["Project", s.project_ok === null ? "" : s.project_ok ? "Found" : "Not found"]]),
          s.error ? h("p", { class: "conn-msg bad" }, s.error) : null),
        section("Repository",
          h("div", { class: "conn-fields" },
            field("ado", "organization", "Organization", { value: v.organization, placeholder: "contoso" }),
            field("ado", "project", "Project", { value: v.project }),
            field("ado", "repository", "Repository", { value: v.repository }),
            field("ado", "target_branch", "Branch", { value: v.target_branch, placeholder: "main" })),
          actions(button("Save", async () => {
            await put("ado", { organization: val("ado", "organization"), project: val("ado", "project"),
              repository: val("ado", "repository"), target_branch: val("ado", "target_branch") });
            for (const k of Object.keys(drafts)) if (k.startsWith("f-ado-")) delete drafts[k];
            await load();
          }, "primary", need(filled(["ado", "organization"], ["ado", "project"], ["ado", "repository"]),
            changed(["organization", "project", "repository", "target_branch"].map((k) => [["ado", k], k === "target_branch" ? v[k] || "" : v[k]])))))),
      ];
    } },
  { id: "jfrog", name: "JFrog", category: "artifacts",
    about: "Admin access for JFrog writes. Without a token the console acts as its operator identity.",
    scope: (s) => (s.url || "").replace(/^https:\/\//, ""),
    panel: (s) => [
      section("Connection", facts([["Platform", s.url], ["Admin token", s.token_present ? "Present" : ""],
        ["Admin rights", s.admin === null || s.admin === undefined ? "" : s.admin ? "Yes" : "No"], ["Console operator", s.operator]]),
        s.error ? h("p", { class: "conn-msg bad" }, s.error) : null, loginSwitch("jfrog", s),
        s.logins?.console ? null : h("div", { class: "conn-fields" },
          field("jfrog", "url", "Platform URL", { type: "url", value: s.url, placeholder: "https://acme.jfrog.io" }),
          field("jfrog", "token", "Admin access token", { type: "password", placeholder: "Paste a token",
            tip: "Kept in this console's memory only: never saved, logged or shown. Enter it again after a restart." })),
        signInOut("jfrog", s, async () => {
          await post("jfrog", "signin", { url: val("jfrog", "url"), token: $("f-jfrog-token")?.value || "" });
          delete drafts["f-jfrog-url"];
        }, need(() => (/^https:\/\/[A-Za-z0-9.-]+(:\d+)?\/?$/.test(val("jfrog", "url")) ? "" : "Enter the platform URL (https://…)"),
          () => ($("f-jfrog-token")?.value ? "" : "Paste an admin token")))),
    ] },
];

// [dot class, text, filter key]
function statusOf(s) {
  if (!s) return ["", "Unknown", "off"];
  if (s.pending?.state === "waiting") return ["warn", "Signing in", "pending"];
  if (s.pending?.state === "failed") return ["fail", "Sign-in failed", "error"];
  if (s.signed_in && s.error) return ["warn", "Needs attention", "error"];
  if (s.signed_in) return ["ok", "Connected", "ok"];
  if (s.error) return ["fail", "Error", "error"];
  return ["", "Not connected", "off"];
}
const STATUSES = [["ok", "Connected"], ["off", "Not connected"], ["pending", "Signing in"], ["error", "Needs attention"]];

// ------------------------------------------------------------------------------------------- page

const svg = (d) => { const s = h("span", { class: "bubble" }); s.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`; return s; };
function stats(items) {
  const n = (k) => items.filter((c) => statusOf(state[c.id])[2] === k).length;
  const ok = n("ok"), here = items.filter((c) => state[c.id]?.source === "console").length;
  const pct = items.length ? Math.round((ok / items.length) * 100) : 0;
  const tile = (cls, mark, num, label, filter) => {
    const t = h("button", { type: "button", class: `hstat ${cls}${F.status === filter && filter ? " on" : ""}` }, mark, h("span", {}, h("b", {}, String(num)), h("small", {}, label)));
    t.addEventListener("click", () => { F.status = F.status === filter ? "" : filter; saveQuery(); draw(); });
    return t;
  };
  const ring = h("span", { class: "ring" }, h("b", {}, `${pct}%`));
  ring.style.setProperty("--p", String(pct));
  return [
    tile("", ring, ok, "Connected", "ok"),
    tile("peach", svg("M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8 12h8"), n("off"), "Not connected", "off"),
    tile("lilac", svg("M12 3l9.5 17h-19zM12 10v4M12 17v.5"), n("error") + n("pending"), "Needs attention", "error"),
    tile("sky", svg("M4 11l8-7 8 7v9h-5v-6H9v6H4z"), here, "Signed in on this console", ""),
  ];
}

function matches(c) {
  const s = state[c.id] || {};
  if (F.category && c.category !== F.category) return false;
  if (F.status && statusOf(s)[2] !== F.status && !(F.status === "error" && statusOf(s)[2] === "pending")) return false;
  if (!F.q) return true;
  const hay = [c.name, CATEGORY[c.category], s.account, c.scope(s), statusOf(s)[1], SOURCE[s.source]].join(" ").toLowerCase();
  return F.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

function row(c) {
  const s = state[c.id] || {};
  const [dot, text] = statusOf(s);
  const name = h("a", { href: `?open=${c.id}` }, c.name);
  name.addEventListener("click", (e) => { e.preventDefault(); openPanel(c.id); });
  const tr = h("tr", { class: `row-link${open === c.id ? " is-open" : ""}`, tabindex: "0" },
    h("td", {}, h("span", { class: "blade-name" }, h("span", { class: "blade-ico" }, logo(c.id)), name)),
    h("td", {}, h("span", { class: `blade-state ${dot}` }, text)),
    h("td", { class: s.account ? "" : "muted" }, s.account || "—"),
    h("td", { class: c.scope(s) ? "" : "muted" }, c.scope(s) || "—"),
    h("td", { class: s.source ? "" : "muted" }, SOURCE[s.source] || "—"),
    h("td", { class: "conn-go" }, h("button", { type: "button", class: "secondary small", tabindex: "-1" },
      s.signed_in ? "Manage" : "Connect")));
  tr.addEventListener("click", (e) => { if (!e.target.closest("a")) openPanel(c.id); });
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPanel(c.id); } });
  return tr;
}

function list() {
  const shown = CONNECTORS.filter(matches);
  $("set-count").textContent = `Showing ${shown.length} of ${CONNECTORS.length} connections`;
  if (!shown.length) return h("p", { class: "blade-empty" }, "No connections match the filter.");
  const body = h("tbody", {});
  for (const [cat, title] of Object.entries(CATEGORY)) {
    const rows = shown.filter((c) => c.category === cat);
    if (!rows.length) continue;
    body.append(h("tr", { class: "conn-group" }, h("th", { colspan: "6", scope: "rowgroup" }, title, h("span", { class: "muted" }, ` ${rows.length}`))));
    for (const c of rows) body.append(row(c));
  }
  return h("table", { class: "blade-table conn-list" },
    h("thead", {}, h("tr", {}, ...["Name", "Status", "Account", "Scope", "Login", ""].map((t) => h("th", { scope: "col" }, t)))), body);
}

function panel() {
  const fly = $("flyout");
  const c = CONNECTORS.find((x) => x.id === open);
  if (!c || !state) { fly.hidden = true; return; }
  const s = state[c.id] || {};
  const [dot, text] = statusOf(s);
  fly.hidden = false;
  $("flyout-title").replaceChildren(logo(c.id), h("span", {}, c.name));
  $("flyout-status").replaceChildren(h("span", { class: `blade-state ${dot}` }, text));
  $("flyout-body").replaceChildren(h("p", { class: "conn-about" }, c.about), ...c.panel(s).filter(Boolean));
  gate();
}

function openPanel(id) {
  open = id;
  saveQuery();
  draw();
  $("flyout-close")?.focus();
}
function closePanel() { open = null; saveQuery(); draw(); }

function draw() {
  if (!state) return;
  const keep = document.activeElement?.id;
  $("set-stats").replaceChildren(...stats(CONNECTORS));
  $("set-filters").replaceChildren(window.consoleBlade.bar({ placeholder: "Filter connections…", q: F.q,
    onQuery: (v) => { F.q = v; saveQuery(); $("set-list").replaceChildren(list()); },
    pills: [
      { label: "Category", value: F.category, options: Object.entries(CATEGORY), onChange: (v) => { F.category = v; saveQuery(); draw(); } },
      { label: "Status", value: F.status, options: STATUSES, onChange: (v) => { F.status = v; saveQuery(); draw(); } },
    ] }));
  $("set-list").replaceChildren(list());
  $("set-list").setAttribute("aria-busy", "false");
  panel();
  if (keep && keep !== "set-q") $(keep)?.focus();
}

async function load() {
  clearTimeout(timer);
  try {
    const s = await api("/api/settings", { abs: true });
    state = { ...s, choices: state?.choices };
    draw();
    if (CONNECTORS.some((c) => s[c.id]?.pending?.state === "waiting")) timer = setTimeout(load, 2500);
  } catch (e) {
    showError(e.status === 401 ? "Signed out of the console: open it again from the link the portal printed." : e.message);
  }
}

open = CONNECTORS.some((c) => c.id === u0.get("open")) ? u0.get("open") : null;
$("set-refresh").addEventListener("click", () => load());
$("flyout-close").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && open && !document.querySelector(".blade-pop")) closePanel(); });
load();
