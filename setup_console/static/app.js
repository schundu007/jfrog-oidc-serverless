// Setup: deploy and destroy test infrastructure. Check reads the stack's outputs; Deploy and Destroy
// plan first, show the plan, and run it only after Approve (destroy also needs the stack name typed).
import { $, api, h, hasToken, setToken, statusPill, flyout, closeFlyout, armed } from "/serverless/static/core.js";

const POLL_MS = 1000;
const fill = (el, ...kids) => { el.replaceChildren(...kids.flat().filter(Boolean)); return el; };

async function waitJob(id, onTick) {
  for (;;) {
    const j = await api(`/api/jobs/${id}`);
    onTick?.(j);
    if (j.state !== "running") return j;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

// Terraform's own output, streamed while a job runs; open while running or after a failure.
// The "$ command → exit" bookkeeping lines are left out, and the box stays hidden until there is output.
const output = (lines) => (lines || []).filter((l) => !l.startsWith("$ "));
function logBox(lines, open = true) {
  const pre = h("pre", { class: "pf-log" });
  const box = h("details", { class: "pf-log-box", open }, h("summary", { text: "Terraform log" }), pre);
  box.update = (next) => {
    const shown = output(next);
    box.hidden = !shown.length;
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
    pre.textContent = shown.join("\n");
    if (stick) pre.scrollTop = pre.scrollHeight;
  };
  box.update(lines);
  return box;
}

const RUNNING = { status: "Reading the stack's Terraform state…", "plan-apply": "Planning the deploy…",
  "plan-destroy": "Planning the destroy…", apply: "Applying the approved plan…" };

function stackStatus(s) {
  if (s.missing?.length) return statusPill("notrun", "Inputs needed");
  if (s.busy) return statusPill("running", "Running");
  if (s.plan) return statusPill("waiting_approval", s.plan.action === "destroy" ? "Destroy planned" : "Deploy planned");
  if (!s.status) return statusPill("notrun", "Not checked");
  return s.status.deployed ? statusPill("succeeded", "Deployed") : statusPill("notrun", "Not deployed");
}

// Readable names for the stacks' outputs; the Terraform name stays as the row's tooltip.
const OUTPUT_LABELS = {
  gcp_workload_identity_provider: "Workload identity provider", gcp_service_accounts: "Pipeline service account",
  tfstate_bucket: "Terraform state bucket", azure_tfstate: "Terraform state", jfrog_provider_name: "JFrog OIDC integration",
  azure_setup_identity_client_id: "Setup identity (client ID)", azure_setup_identity_principal_id: "Setup identity (object ID)",
  workload_service_account: "Workload service account", image_repository: "Image repository", job_name: "Job",
  cluster_name: "Cluster", location: "Location", oidc_issuer_url: "OIDC issuer", kubeconfig_command: "Credentials command",
  acr_login_server: "Container registry", workload_identity_client_id: "Workload identity (client ID)", resource_group: "Resource group",
};
const outputLabel = (k) => OUTPUT_LABELS[k] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

// Scalars as one row each; a map or object (service accounts, state location) as one row per entry.
function outputsTable(outputs) {
  const rows = [];
  for (const [k, v] of Object.entries(outputs || {})) {
    if (v === null || v === undefined) continue;
    if (typeof v !== "object") { rows.push([outputLabel(k), k, v]); continue; }
    for (const [sk, sv] of Object.entries(v)) {
      if (sv !== null && typeof sv !== "object") rows.push([`${outputLabel(k)}: ${sk.replace(/_/g, " ")}`, `${k}.${sk}`, sv]);
    }
  }
  if (!rows.length) return null;
  return h("table", { class: "setup-outputs" }, h("tbody", {}, rows.map(([label, name, v]) =>
    h("tr", { title: name }, h("th", { text: label }), h("td", { class: "mono", text: String(v) })))));
}

// What a Check or an apply found, in words: the state, what the stack is, its values, what to do next.
function resultView(stack, result) {
  if (result?.destroyed) return h("p", {}, h("b", { text: "Destroyed." }), " Nothing of this stack is left in its Terraform state.");
  if (!result?.deployed && !result?.applied) {
    return [h("p", {}, h("b", { text: "Not deployed." }), " This stack has no Terraform state yet."),
      h("p", { text: `Deploy creates: ${stack.about}` })];
  }
  return [h("p", {}, h("b", { text: "Deployed." }), ` ${stack.about}`),
    outputsTable(result.outputs),
    stack.next ? h("p", {}, h("b", { text: "Next: " }), stack.next) : null];
}

function planView(stack, plan, panel) {
  const counts = Object.entries(plan.summary || {}).filter(([k, n]) => n && k !== "no-op" && k !== "read").map(([k, n]) => `${n} ${k}`);
  const typed = plan.destructive?.length ? h("input", { type: "text", placeholder: plan.confirmation_phrase, "aria-label": "Type the stack name", spellcheck: "false" }) : null;
  const approve = h("button", { type: "button", class: "primary", text: plan.action === "destroy" ? "Approve and destroy" : "Approve and deploy" });
  const discard = h("button", { type: "button", class: "secondary", text: "Discard" });
  // Destroying or replacing needs the stack name typed: Approve stays greyed out until it matches.
  if (typed) {
    const sync = () => { approve.disabled = typed.value.trim() !== plan.confirmation_phrase; };
    typed.addEventListener("input", sync);
    sync();
  }
  approve.onclick = async () => {
    approve.disabled = true;
    try {
      const job = await api(`/api/stacks/${stack.id}/jobs`, { method: "POST",
        body: { action: "apply", approval: { plan_sha256: plan.sha256, typed_confirmation: typed ? typed.value : undefined } } });
      await follow(stack, job, panel);
    } catch (e) { approve.disabled = typed ? typed.value.trim() !== plan.confirmation_phrase : false; fill(panel.status, statusPill("failed", e.message)); }
  };
  discard.onclick = async () => { await api(`/api/stacks/${stack.id}/plan`, { method: "DELETE" }); closeFlyout(); load(); };
  const changes = plan.changes?.length
    ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, plan.changes.map((c) => h("tr", {}, h("td", { text: c.action }), h("td", { text: c.address }))))))
    : h("p", { text: "No changes" });
  return [h("p", {}, h("b", { text: counts.join(", ") || "No changes" })), changes,
    h("div", { class: "actions setup-actions" }, typed, plan.changes?.length ? approve : null, discard)];
}

async function follow(stack, job, panel) {
  const box = logBox(job.log);
  fill(panel.body, h("p", { class: "muted", text: RUNNING[job.action] || "Running…" }), box);
  fill(panel.status, statusPill("running", "Running"));
  const done = await waitJob(job.id, (j) => box.update(j.log));
  load();
  if (done.state === "failed") {
    fill(panel.status, statusPill("failed", "Failed"));
    return fill(panel.body, h("pre", { class: "pf-log" }, done.error || ""), logBox(done.log));
  }
  fill(panel.status, statusPill("succeeded", "Done"));
  if (done.action.startsWith("plan-")) return fill(panel.body, planView(stack, done.result, panel), logBox(done.log, false));
  fill(panel.body, resultView(stack, done.result), logBox(done.log, false));
}

async function run(stack, action) {
  const panel = flyout(`${stack.group} · ${stack.title}: ${{ status: "Check", "plan-apply": "Deploy", "plan-destroy": "Destroy" }[action]}`);
  try { await follow(stack, await api(`/api/stacks/${stack.id}/jobs`, { method: "POST", body: { action } }), panel); }
  catch (e) { fill(panel.status, statusPill("failed", e.message)); }
}

// One field per variable the stack declares. Pickers list live projects / subscriptions; any
// value can still be typed. JSON-typed variables (objects, maps, lists) take JSON.
const optionCache = {};
async function options(source) {
  optionCache[source] ||= api(`/api/options/${source}`).then((r) => r.options).catch(() => []);
  return optionCache[source];
}

function input(f) {
  const id = `in-${f.name}`;
  if (f.kind === "bool") return h("input", { id, type: "checkbox", checked: f.current === true });
  if (f.kind === "json") {
    return h("textarea", { id, class: "setup-json", spellcheck: "false", rows: 4 },
      f.current === undefined ? "" : JSON.stringify(f.current, null, 2));
  }
  const el = h("input", { id, type: f.kind === "number" ? "number" : "text", spellcheck: "false",
    value: f.current ?? "", required: f.required });
  if (f.options) {
    const list = h("datalist", { id: `${id}-list` });
    el.setAttribute("list", list.id);
    options(f.options).then((opts) => fill(list, opts.map((o) => h("option", { value: o.id, text: o.name }))));
    return [el, list];
  }
  return el;
}

function readField(f) {
  const el = $(`in-${f.name}`);
  if (f.kind === "bool") return el.checked;
  if (f.kind === "number") return el.value === "" ? null : Number(el.value);
  return el.value;
}

async function editInputs(stack) {
  const panel = flyout(`${stack.group} · ${stack.title}: Inputs`);
  const draw = (form) => {
    const fields = form.fields.map((f) => h("label", { class: "field" },
      h("span", {}, h("span", { class: "field-label", text: f.name }), f.required ? h("span", { class: "small", text: "required" }) : null),
      input(f),
      f.description ? h("span", { class: "field-msg", text: f.description }) : null));
    const save = h("button", { type: "submit", class: "primary", text: "Save" });
    const el = h("form", { class: "setup-form", autocomplete: "off" }, fields, h("div", { class: "actions setup-actions" }, save));
    el.onsubmit = async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        const values = Object.fromEntries(form.fields.map((f) => [f.name, readField(f)]));
        const next = await api(`/api/stacks/${stack.id}/inputs`, { method: "PUT", body: { values } });
        load();
        // Complete: close. Still missing a required input: stay open on it.
        if (!next.missing.length) return closeFlyout();
        fill(panel.status, statusPill("waiting_approval", `Missing: ${next.missing.join(", ")}`));
        document.getElementById(`in-${next.missing[0]}`)?.focus();
      } catch (err) { fill(panel.status, statusPill("failed", err.message)); }
      save.disabled = false;
    };
    fill(panel.body, el);
  };
  try { draw(await api(`/api/stacks/${stack.id}/inputs`)); }
  catch (e) { fill(panel.status, statusPill("failed", e.message)); }
}

function row(s) {
  const destroy = h("button", { type: "button", class: "secondary", text: "Destroy", disabled: s.busy });
  destroy.onclick = () => { if (armed(destroy, "Plan destroy?")) run(s, "plan-destroy"); };
  const plan = s.plan ? h("button", { type: "button", class: "primary", text: "Review plan" }) : null;
  if (plan) plan.onclick = () => { const p = flyout(`${s.group} · ${s.title}`); fill(p.body, planView(s, s.plan, p)); };
  return h("tr", {},
    h("td", { title: `terraform/stacks/${s.id}` }, h("b", { text: s.title })),
    h("td", {}, stackStatus(s)),
    h("td", { class: "setup-row-actions" },
      plan,
      h("button", { type: "button", class: "secondary", text: "Inputs", onclick: () => editInputs(s) }),
      h("button", { type: "button", class: "secondary", text: "Check", disabled: s.busy, onclick: () => run(s, "status") }),
      h("button", { type: "button", class: "primary", text: "Deploy", disabled: s.busy, onclick: () => run(s, "plan-apply") }),
      destroy));
}

async function load() {
  const data = await api("/api/stacks");
  $("env").textContent = data.env;
  const groups = {};
  for (const s of data.stacks) (groups[s.group] ||= []).push(s);
  fill($("groups"), Object.entries(groups).map(([g, list]) => h("section", { class: "card" },
    h("div", { class: "card-head" }, h("h2", { text: g })),
    h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, list.map(row)))))));
}

$("flyout-close").onclick = closeFlyout;
$("auth-form").onsubmit = (e) => {
  e.preventDefault();
  const m = $("auth-input").value.match(/#t=([A-Za-z0-9_-]+)/);
  if (m) { setToken(m[1]); $("auth-banner").hidden = true; load(); }
};
if (!hasToken()) $("auth-banner").hidden = false;
else load().catch((e) => { if (e.status === 401) $("auth-banner").hidden = false; });
