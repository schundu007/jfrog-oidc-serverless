// Setup: deploy and destroy test infrastructure. Check reads the stack's outputs; Deploy and Destroy
// plan first, show the plan, and run it only after Approve (destroy also needs the stack name typed).
import { $, api, h, hasToken, setToken, statusPill, flyout, closeFlyout, help, menu, listEditor, mapEditor, markKnown, refreshing } from "/serverless/static/core.js";
import { logBox as tableLog } from "/serverless/static/logview.js";
import { counts, lastFailure, partlyDeleted, standing, stateOf } from "./listing.js";
import { clock, dur } from "/serverless/static/time.js";

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

// Terraform's own output as a Time | Message table, streamed while a job runs; open while running or
// after a failure. The "$ command → exit" bookkeeping lines are left out; hidden until there is output.
const rows = (job) => (job?.log || []).map((text, i) => ({ t: job.log_times?.[i], text })).filter((r) => !r.text.startsWith("$ "));
function logBox(job, open = true) {
  const box = tableLog("Terraform log", [], open);
  let shown = [];
  box.update = (j) => {
    const next = rows(j);
    const grows = next.length >= shown.length && shown.every((r, i) => r.text === next[i].text && r.t === next[i].t);
    if (grows) next.slice(shown.length).forEach(box.add); else box.set(next);
    shown = next;
  };
  box.update(job);
  return box;
}

// What a run is, named by what it does: a plan, or the approved apply or destroy.
const jobTitle = (j) => ({ status: "Check state", "plan-apply": "Plan deploy", "plan-destroy": "Plan destroy", forget: "Forget state" }[j?.action]
  || (j?.op === "destroy" ? "Destroy" : "Deploy"));

// The run's stages, one row each: terraform init, terraform plan, Approval, terraform apply or destroy,
// then a workload's build-deploy and onboarding. Updated live while the run goes on.
const STAGE_TEXT = { succeeded: "Succeeded", failed: "Failed", running: "Running", waiting: "Waiting", pending: "Not started", skipped: "Skipped" };
const STAGE_TONE = { succeeded: "ok", failed: "fail", running: "warn", waiting: "warn" };
const at = (sec) => (sec ? clock(new Date(sec * 1000).toISOString()) : "—");
function stagesView(job) {
  const tbody = h("tbody");
  const box = h("div", { class: "table-wrap setup-stages" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Stage", "Status", "Started", "Duration", "Run"].map((t) => h("th", { text: t })))), tbody));
  box.update = (j) => {
    box.hidden = !j.stages?.length;
    fill(tbody, (j.stages || []).map((st) => h("tr", { "data-state": st.state },
      h("td", { class: "stage-name", text: st.name }),
      h("td", {}, st.state === "running" ? statusPill("running", "Running")
        : dot(STAGE_TONE[st.state] || "", st.state === "waiting" && st.name === "Approval" ? "Waiting for approval" : STAGE_TEXT[st.state] || st.state)),
      h("td", { text: at(st.started) }),
      h("td", { text: st.started ? dur(((st.finished || Date.now() / 1000) - st.started) * 1000) : "—" }),
      h("td", {}, st.url ? h("a", { href: st.url, target: "_blank", rel: "noopener", text: "ADO run" }) : "—"))));
  };
  box.update(job);
  return box;
}

// The row's status while a run goes on: the stage it is in, in words.
function busyText(j) {
  const plan = j?.op === "destroy" ? "Planning destroy" : "Planning deploy";
  return { "terraform init": j?.action === "status" ? "Checking…" : plan, "terraform plan": plan, Approval: "Waiting for approval",
    "terraform apply": "Applying", "terraform destroy": "Destroying", "terraform output": "Checking…",
    "Build and deploy": "Building and deploying", "Onboarding and post-deployment check": "Onboarding" }[j?.stage]
    || (j?.op === "destroy" ? "Destroying" : "Deploying");
}

// Status as on Home: a coloured dot and plain words (green deployed, amber needs you or running, grey idle).
const dot = (tone, text) => h("span", { class: `blade-state ${tone}`, text });
function stackStatus(s) {
  if (s.missing?.length) return dot("warn", "Inputs needed");
  if (s.busy) return h("span", { title: s.job?.stage || "" }, dot("warn", busyText(s.job)));
  if (s.checking) return dot("", "Checking…");
  if (s.plan) return dot("warn", s.plan.action === "destroy" ? "Destroy planned" : "Deploy planned");
  // Waiting on the stacks it stands on (all of them, upstream first): say which.
  if (s.needs?.length && !s.status?.deployed) return dot("", `Needs ${s.needs.map(titleOf).join(", ")}`);
  if (!s.status) return dot("", "Checking…");
  if (s.status.deployed) return dot("ok", "Deployed");
  // Deleted outside the console, validated with the cloud: the evidence is behind the row's "?".
  if (s.status.deleted) return partlyDeleted(s) ? dot("warn", "Partly deleted") : dot("", "Deleted");
  // A status read that failed (e.g. no state bucket yet) also means nothing is deployed; the reason is behind the "?".
  return lastFailure(s) ? dot("fail", "Not deployed") : dot("", "Not deployed");
}

// Readable names for the stacks' outputs; the Terraform name stays as the row's tooltip.
const OUTPUT_LABELS = {
  gcp_workload_identity_provider: "Workload identity provider", gcp_service_accounts: "Pipeline service account",
  tfstate_bucket: "Terraform state bucket", azure_tfstate: "Terraform state", jfrog_provider_name: "JFrog OIDC integration",
  azure_setup_identity_client_id: "Setup identity (client ID)", azure_setup_identity_principal_id: "Setup identity (object ID)",
  workload_service_account: "Workload service account", image_repository: "Image repository", job_name: "Job",
  location: "Location",
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
  if (result?.destroyed) return h("p", {}, h("b", { text: "Destroyed" }), help("Nothing of this stack is left in its Terraform state."));
  if (result?.applied && result?.build) return null;   // the steps table says it
  if (!result?.deployed && !result?.applied) {
    return h("p", {}, h("b", { text: "Not deployed" }), help(`No Terraform state yet. Deploy creates: ${stack.about}`));
  }
  return [h("p", {}, h("b", { text: "Deployed" }), help(stack.next ? `${stack.about} Next: ${stack.next}` : stack.about)),
    outputsTable(result.outputs)];
}

// A Deploy goes all the way: Infrastructure, Build and deploy, Onboard. One row per step.
function chainView(result) {
  if (!result?.build && !result?.onboard) return null;
  const word = { done: "Done", started: "Running", blocked: "Waiting", needs_input: "Needs you", failed: "Failed" };
  const pill = { done: "succeeded", started: "running", blocked: "waiting_approval", needs_input: "waiting_approval", failed: "failed" };
  const step = (name, st, link, text) => h("tr", {},
    h("td", { text: name }), h("td", {}, statusPill(pill[st] || "running", word[st] || st), text ? help(text) : null),
    h("td", {}, link ? h("a", { href: link, target: link.startsWith("http") ? "_blank" : null, rel: "noopener", text: "Open" }) : ""));
  return h("div", { class: "table-wrap" }, h("table", { class: "chain" },
    h("thead", {}, h("tr", {}, h("th", { text: "Step" }), h("th", { text: "Status" }), h("th", { text: "" }))),
    h("tbody", {},
      step("Infrastructure", "done", result.run),
      result.build ? step("Build and deploy", result.build.state, result.build.run, result.build.text || (result.build.again ? "First image in: Deploy again to start it." : "")) : null,
      result.onboard ? step("Onboard", result.onboard.state, result.onboard.link, result.onboard.text) : null)));
}

function planView(stack, plan, panel, wiz) {
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
      if (wiz) panel = wizPanel(wiz, "Deploy");
      await follow(stack, job, panel, wiz);
    } catch (e) { approve.disabled = typed ? typed.value.trim() !== plan.confirmation_phrase : false; fill(panel.status, statusPill("failed", e.message)); }
  };
  if (wiz) discard.textContent = "Back to settings";
  discard.onclick = async () => {
    await api(`/api/stacks/${stack.id}/plan`, { method: "DELETE" });
    if (wiz) return editInputs(stack, wiz);
    closeFlyout(); load();
  };
  const changes = plan.changes?.length
    ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, plan.changes.map((c) => h("tr", {}, h("td", { text: c.action }), h("td", { text: c.address }))))))
    : h("p", { text: "No changes" });
  return [h("p", {}, h("b", { text: counts.join(", ") || "No changes" })), changes,
    h("div", { class: "actions setup-actions" }, typed, plan.changes?.length ? approve : null, discard)];
}

async function follow(stack, job, panel, wiz) {
  const box = logBox(job);
  const stages = stagesView(job);
  $("flyout-title").textContent = `${stack.group} · ${stack.title}: ${jobTitle(job)}`;
  fill(panel.body, stages, box);
  fill(panel.status, statusPill("running", "Running"));
  const done = await waitJob(job.id, (j) => { box.update(j); stages.update(j); });
  load();
  if (done.state === "failed") {
    fill(panel.status, statusPill("failed", "Failed"));
    return fill(panel.body, stagesView(done), nextView(stack, done.next), h("pre", { class: "pf-log" }, done.error || ""),
      wiz ? wizNav(wiz, stack, "plan") : null, logBox(done));
  }
  if (done.result?.forgotten) return fill(panel.body, h("p", {}, h("b", { text: "State forgotten" }),
    help("The stack shows as not deployed; the old state file is kept aside. Deploy builds it again from nothing.")), logBox(done, false));
  fill(panel.status, statusPill("succeeded", "Done"));
  // Validated as deleted outside the console: nothing to plan or approve.
  if (done.result?.already_deleted) return fill(panel.body, h("p", {}, h("b", { text: done.result.message })), logBox(done, false));
  const chain = chainView(done.result);
  const onboard = done.result?.onboard;
  const after = chain ? h("div", { class: "actions setup-actions" },
    onboard?.link && onboard.state !== "done" ? h("a", { class: "button primary", href: onboard.link, text: onboard.state === "failed" ? "Onboarding log" : "Open onboarding" }) : null,
    h("button", { type: "button", class: "secondary", text: "Close", onclick: () => { closeFlyout(); load(); } })) : null;
  if (done.action.startsWith("plan-")) {
    if (done.result?.no_changes && chain) return fill(panel.body, stagesView(done), h("p", {}, h("b", { text: "No infrastructure changes" })), chain, after, logBox(done, false));
    return fill(panel.body, stagesView(done), planView(stack, done.result, panel, wiz), logBox(done, false));
  }
  fill(panel.body, stagesView(done), resultView(stack, done.result), chain, after, logBox(done, false));
}

async function run(stack, action, approval, wiz) {
  const verb = { status: "Check", "plan-apply": "Deploy", "plan-destroy": "Destroy", forget: "Forget state" }[action];
  const panel = wiz ? wizPanel(wiz, "Plan") : flyout(`${stack.group} · ${stack.title}: ${verb}`);
  try { await follow(stack, await api(`/api/stacks/${stack.id}/jobs`, { method: "POST", body: { action, approval } }), panel, wiz); }
  catch (e) {
    fill(panel.status, statusPill("failed", "Failed"));
    if (wiz) fill(panel.body, h("p", { class: "wiz-error", text: e.message }), wizNav(wiz, stack, "plan"));
    else fill(panel.status, statusPill("failed", e.message));
  }
}

// ---- Create: another deployment of a workload stack, one step at a time --------------------------
// Name (checked here and in Azure) → Settings → Plan → Deploy. Every step has Back; a failure says
// why and offers Try again and Back to settings; nothing is left half made (Back from Settings
// forgets a deployment that was never deployed).
const WIZ_STEPS = ["Name", "Settings", "Plan", "Deploy"];
const WHERE_FIRST = ["azure_subscription_id", "gcp_project_id", "azure_location", "gcp_region", "workload"];
function wizPanel(wiz, step) {
  const steps = wiz.added ? WIZ_STEPS : WIZ_STEPS.slice(1);
  const panel = flyout(`Create ${wiz.base.title}${wiz.name ? ` · ${wiz.name}` : ""}`);
  const at = steps.indexOf(step);
  panel.steps = h("ol", { class: "wiz-steps" }, steps.map((t, i) =>
    h("li", { class: i < at ? "done" : i === at ? "current" : "", text: `${i + 1}. ${t}` })));
  const body = panel.body;
  const inner = h("div", { class: "wiz-body" });
  fill(body, panel.steps, inner);
  return { status: panel.status, body: inner };
}
function wizNav(wiz, stack, where) {
  const b = (text, cls, onclick) => h("button", { type: "button", class: cls, text, onclick });
  return h("div", { class: "actions setup-actions" },
    where === "plan" ? b("Try again", "primary", () => run(stack, "plan-apply", undefined, wiz)) : null,
    b("Back to settings", "secondary", () => editInputs(stack, wiz)),
    b("Close", "secondary", () => { closeFlyout(); load(); }));
}

async function createFlow(baseId) {
  await (lastData ? Promise.resolve() : load());
  const base = byId[baseId];
  if (!base) return;
  const family = lastData.stacks.filter((s) => s.id === baseId || s.id.startsWith(`${baseId}@`));
  // The stack's own deployment first: while it stands nowhere, Create fills it in; after that, a new name.
  const wiz = { base, added: Boolean(base.status?.deployed || base.busy || base.plan) };
  if (base.needs?.length) {
    const panel = flyout(`Create ${base.title}`);
    return fill(panel.body, h("p", { class: "wiz-error", text: `Needs ${base.needs.map(titleOf).join(", ")} deployed first.` }),
      h("div", { class: "actions setup-actions" }, h("button", { type: "button", class: "secondary", text: "Close", onclick: closeFlyout })));
  }
  if (!wiz.added) return editInputs(base, wiz);
  return nameStep(wiz, family);
}

function nameStep(wiz, family, value = "") {
  wiz.name = "";
  const panel = wizPanel(wiz, "Name");
  const name = h("input", { id: "wiz-name", type: "text", value, spellcheck: "false", autocomplete: "off", "aria-label": "Name" });
  const said = h("p", { class: "small wiz-check" });
  const next = h("button", { type: "submit", class: "primary", text: "Next", disabled: true });
  const cancel = h("button", { type: "button", class: "secondary", text: "Cancel", onclick: closeFlyout });
  let seq = 0, verdict = null;
  const check = async () => {
    const v = name.value.trim().toLowerCase(), mine = ++seq;
    verdict = null;
    next.disabled = true;
    name.classList.remove("is-known", "is-unknown");
    if (!v) return fill(said);
    fill(said, "Checking…");
    try {
      const r = await api(`/api/stacks/${wiz.base.id}/name-check?name=${encodeURIComponent(v)}`);
      if (mine !== seq) return;
      verdict = r;
      name.classList.toggle("is-known", r.ok === true);
      name.classList.toggle("is-unknown", r.ok === false);
      fill(said, r.ok === true ? "Free" : r.ok === false ? r.why : `Not checked in the cloud: ${r.why}`);
      next.disabled = r.ok === false;
    } catch (e) { if (mine === seq) fill(said, e.message); }
  };
  let timer;
  name.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(check, 350); });
  const form = h("form", { class: "setup-form", autocomplete: "off" },
    h("label", { class: "field" }, h("span", {}, h("span", { class: "field-label", text: "Name" }),
      help(`The new ${wiz.base.title}'s name; also its resource names. Must be new here and, for Azure, free in Azure. `
        + `In use: ${family.map((s) => s.id.split("@")[1] || s.status?.outputs?.app_name || s.status?.outputs?.job_name || s.title).join(", ")}.`)), name, said),
    h("div", { class: "actions setup-actions" }, next, cancel));
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (verdict?.ok === false || !name.value.trim()) return;
    next.disabled = true;
    try {
      const made = await api(`/api/stacks/${wiz.base.id}/instances`, { method: "POST", body: { name: name.value.trim().toLowerCase() } });
      wiz.name = made.id.split("@")[1];
      wiz.made = made.id;
      await load();
      editInputs(byId[made.id], wiz);
    } catch (e) { fill(said, e.message); name.classList.add("is-unknown"); next.disabled = false; }
  };
  fill(panel.body, form);
  name.focus();
  if (value) check();
}

// What a failed run suggests: the likely cause in words, then the actions that fix it (first = most
// likely). Forget state touches nothing in the cloud but needs the stack name typed.
function nextActions(s, next, { withForget = true } = {}) {
  return (next?.actions || []).map((a) => {
    if (a.action !== "forget") return h("button", { type: "button", class: "secondary small-btn", text: a.label, onclick: () => run(s, a.action) });
    if (!withForget) return null;
    const typed = h("input", { type: "text", placeholder: s.id, "aria-label": `Type ${s.id} to forget its state`, spellcheck: "false" });
    const go = h("button", { type: "button", class: "secondary danger", text: a.label, disabled: true });
    typed.oninput = () => { go.disabled = typed.value.trim() !== s.id; };
    go.onclick = () => run(s, "forget", { typed_confirmation: typed.value.trim() });
    return [typed, go];
  });
}

function nextView(s, next) {
  if (!next) return null;
  return h("div", { class: "setup-next" },
    h("div", { class: "actions setup-actions" }, nextActions(s, next), help(next.cause)));
}

// One field per variable the stack declares. Pickers list live projects / subscriptions; any
// value can still be typed. Lists take one row per item, maps name/value rows, objects one box per
// field: the form never asks for JSON. The description sits behind the field's "?".
const optionCache = {};
// A failed listing resolves to null: no tint, the field stays free text.
async function options(source, scope = "") {
  const key = `${source}?${scope}`;
  optionCache[key] ||= api(`/api/options/${source}${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`)
    .then((r) => r.options).catch(() => null);
  return optionCache[key];
}
// Google Cloud members are listed for the project typed in the same form.
const scopeOf = (source) => (source === "gcp_members" ? $("in-gcp_project_id")?.value.trim() || "" : "");

const LINUX_ONLY = ["python", "php"];

function input(f) {
  const id = `in-${f.name}`;
  if (f.kind === "bool") return h("input", { id, type: "checkbox", checked: f.current === true });
  if (f.kind === "list") {
    const opts = f.options ? new Promise((r) => setTimeout(r)).then(() => options(f.options, scopeOf(f.options))).then((o) => o || Promise.reject()) : null;
    return Object.assign(listEditor(Array.isArray(f.current) ? f.current : [], { label: f.name, placeholder: f.options ? "email" : "", options: opts }), { id });
  }
  if (f.kind === "map") return Object.assign(mapEditor(f.current || {}), { id });
  if (f.kind === "object") {
    const el = h("div", { id, class: "field-sub" }, f.fields.map((k) => h("label", {}, k,
      h("input", { type: "text", "data-key": k, value: f.current?.[k] ?? "", spellcheck: "false" }))));
    el.value = () => {
      const o = Object.fromEntries([...el.querySelectorAll("input")].map((i) => [i.dataset.key, i.value.trim()]));
      return Object.values(o).some(Boolean) ? o : null;
    };
    return el;
  }
  if (f.choices) {
    // A validated list of values (e.g. os, runtime): pick one, nothing to type.
    const cur = f.current ?? "";
    return h("select", { id, required: f.required },
      (f.required || cur ? [] : [h("option", { value: "", text: "" })]).concat(
        f.choices.map((c) => h("option", { value: c, text: c, selected: c === cur }))));
  }
  const el = h("input", { id, type: f.kind === "number" ? "number" : "text", spellcheck: "false",
    value: f.current ?? "", required: f.required, readonly: Boolean(f.locked) });
  if (f.options) {
    const list = h("datalist", { id: `${id}-list` });
    el.setAttribute("list", list.id);
    let known = null;
    const check = markKnown(el, () => known);
    options(f.options).then((opts) => {
      if (!opts) return;
      known = new Set(opts.map((o) => String(o.id).toLowerCase()));
      fill(list, opts.map((o) => h("option", { value: o.id, text: o.name })));
      check();
    });
    return [el, list];
  }
  return el;
}

function readField(f) {
  const el = $(`in-${f.name}`);
  if (f.kind === "bool") return el.checked;
  if (["list", "map", "object"].includes(f.kind)) return el.value();
  if (f.kind === "number") return el.value === "" ? null : Number(el.value);
  return el.value;
}

async function editInputs(stack, wiz) {
  const panel = wiz ? wizPanel(wiz, "Settings") : flyout(`${stack.group} · ${stack.title}: Inputs`);
  const draw = (form) => {
    // A list, map or object is a group of inputs: a div, so its rows' buttons are not the label's target.
    const row = (f0) => { const f = wiz && WHERE_FIRST.includes(f0.name) ? { ...f0, required: true } : f0; return h(["list", "map", "object"].includes(f.kind) ? "div" : "label", { class: "field" },
      h("span", {}, h("span", { class: "field-label", text: f.name }), f.required ? h("span", { class: "small", text: "required" }) : null,
        f.description ? help(f.description) : null),
      input(f)); };
    // Where it goes first: subscription or project, its new resource group, location or region, name.
    const shown = form.fields.filter((f) => f.kind !== "json");
    const rank = (f) => { const i = WHERE_FIRST.indexOf(f.name); return i < 0 ? WHERE_FIRST.length : i; };
    const fields = [...shown].sort((x, y) => rank(x) - rank(y)).flatMap((f) => {
      if (f.name !== "azure_subscription_id") return [row(f)];
      const wl = shown.find((x) => x.name === "workload")?.current || stack.instance || "";
      const rg = h("input", { id: "in-resource-group", type: "text", value: wl ? `rg-${wl}` : "", readonly: true, required: true });
      return [row(f), h("label", { class: "field" }, h("span", {}, h("span", { class: "field-label", text: "resource_group" }),
        h("span", { class: "small", text: "required" }),
        help("A new resource group, rg-<workload>, made by this deployment and holding only its resources. Never an existing one.")), rg)];
    });
    const save = h("button", { type: "submit", class: "secondary", text: "Save", hidden: Boolean(wiz) });
    const deploy = h("button", { type: "submit", class: "primary", text: wiz ? "Next: plan" : "Save and deploy", disabled: stack.busy });
    // Back from Settings: an added deployment never deployed is forgotten, so its name is free again.
    const back = wiz ? h("button", { type: "button", class: "secondary", text: "Back", onclick: async () => {
      if (wiz.made && !byId[wiz.made]?.status?.deployed) {
        try { await api(`/api/stacks/${wiz.made}/instances`, { method: "DELETE" }); } catch { /* kept: shown in the list */ }
        const name = wiz.name;
        wiz.made = null;
        await load();
        return nameStep(wiz, lastData.stacks.filter((s) => s.id === wiz.base.id || s.id.startsWith(`${wiz.base.id}@`)), name);
      }
      closeFlyout();
    } }) : null;
    let andDeploy = false;
    save.onclick = () => { andDeploy = false; };
    deploy.onclick = () => { andDeploy = true; };
    const el = h("form", { class: "setup-form", autocomplete: "off" }, fields, h("div", { class: "actions setup-actions" }, deploy, save, back));
    const wlIn = el.querySelector("#in-workload"), rgIn = el.querySelector("#in-resource-group");
    if (wlIn && rgIn) wlIn.addEventListener("input", () => { rgIn.value = wlIn.value.trim() ? `rg-${wlIn.value.trim().toLowerCase()}` : ""; });
    // Web App: Python and PHP run on Linux only (the stack's precondition), so Windows greys them out.
    const os = el.querySelector("#in-os"), rt = el.querySelector("#in-runtime");
    if (os?.tagName === "SELECT" && rt?.tagName === "SELECT") {
      const sync = () => {
        for (const o of rt.options) o.disabled = os.value === "windows" && LINUX_ONLY.includes(o.value);
        if (rt.selectedOptions[0]?.disabled) rt.value = [...rt.options].find((o) => !o.disabled)?.value ?? "";
      };
      os.addEventListener("change", sync);
      sync();
    }
    el.onsubmit = async (e) => {
      e.preventDefault();
      save.disabled = deploy.disabled = true;
      try {
        const values = Object.fromEntries(form.fields.filter((f) => f.kind !== "json").map((f) => [f.name, readField(f)]));
        const next = await api(`/api/stacks/${stack.id}/inputs`, { method: "PUT", body: { values } });
        load();
        // Complete: close, or go on to the deploy plan. Still missing a required input: stay open on it.
        if (!next.missing.length) return andDeploy ? run(stack, "plan-apply", undefined, wiz) : closeFlyout();
        fill(panel.status, statusPill("waiting_approval", `Missing: ${next.missing.join(", ")}`));
        document.getElementById(`in-${next.missing[0]}`)?.focus();
      } catch (err) { fill(panel.status, statusPill("failed", err.message)); }
      save.disabled = false;
      deploy.disabled = !!stack.busy;
    };
    fill(panel.body, el);
  };
  try { draw(await api(`/api/stacks/${stack.id}/inputs`)); }
  catch (e) { fill(panel.status, statusPill("failed", e.message)); }
}

// A JFrog OIDC row names the infrastructure it stands on, which this page does not list: its title only.
const titleOf = (id) => byId[id] ? byId[id].title : lastData?.titles?.[id] || id;

// Terraform's error, cut to its point: no "terraform x failed (exit 1): Error:" prefix, one short line.
function brief(text, max = 140) {
  const t = String(text || "").replace(/^terraform \S+ failed \(exit \d+\):\s*/, "").replace(/^Error:\s*/, "").split("\n")[0];
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

// What stops or failed this stack, each with where to go: the pipeline identity it needs, the stacks that
// stand on it, the last run that failed. A failed state read is left out when the missing pipeline identity
// already explains it.

// What stops or failed this stack, as plain sentences: shown only behind the circled "?" next to its
// status, never as a banner. A failed state read is left out when the missing pipeline identity explains it.
function warnings(s) {
  const out = [];
  if (s.status?.deleted && !s.busy && !s.plan) out.push(s.status.deleted);
  else if (s.status?.error && !s.needs?.length) out.push(s.status.error);
  if (s.needs?.length) out.push(`Deploy ${s.needs.map(titleOf).join(", ")} first.`);
  if (s.dependents?.length) out.push(`Destroy ${s.dependents.map(titleOf).join(", ")} first.`);
  else if (s.unchecked?.length) out.push(`Destroy waits until ${s.unchecked.map(titleOf).join(", ")} ${s.unchecked.length > 1 ? "are" : "is"} checked.`);
  const failed = lastFailure(s);
  if (failed) out.push(`${jobTitle(failed)} failed: ${brief(failed.error) || "see the log"}`);
  return out;
}

function row(s, section) {
  // Nothing is removed from under another stack: deployed ones block, and so do ones not read yet.
  const blockedDestroy = s.dependents?.length ? `Destroy ${s.dependents.map(titleOf).join(", ")} first`
    : s.unchecked?.length ? `Checking ${s.unchecked.map(titleOf).join(", ")} first` : "";
  const plan = s.plan ? h("button", { type: "button", class: "primary", text: "Review plan" }) : null;
  if (plan) plan.onclick = () => { const p = flyout(`${s.group} · ${s.title}`); fill(p.body, planView(s, s.plan, p)); };
  // A running job keeps going when its drawer is closed or the page reloads: this reopens its live log.
  const progress = s.job ? h("button", { type: "button", class: "primary", text: "Progress" }) : null;
  if (progress) {
    progress.onclick = () => follow(s, s.job, flyout(`${s.group} · ${s.title}: ${jobTitle(s.job)}`));
  }
  const blockedDeploy = s.needs?.length ? `Deploy ${s.needs.map(titleOf).join(", ")} first` : "";
  const notes = warnings(s);
  // A failed run keeps its log one click away; what to do next is in that log's drawer.
  const failed = lastFailure(s);
  const log = failed ? h("button", { type: "button", class: "secondary", text: "Log",
    onclick: () => follow(s, failed, flyout(`${s.group} · ${s.title}: ${jobTitle(failed)}`)) }) : null;
  // The row's state tones its status (green deployed, dark needs you, grey idle); a row waiting on another stack says which.
  const state = s.busy || s.plan ? "busy" : s.status?.deployed ? "ok" : partlyDeleted(s) || lastFailure(s) || s.needs?.length || s.missing?.length ? "attention" : "idle";
  return h("tr", { id: `stack-${s.id}`, "data-state": state, class: s.needs?.length ? "blocked" : null },
    h("td", { title: s.dir }, h("div", { class: "stack-name" }, iconEl(STACK_ICON[s.id], "stack-icon"),
      link(nameHref(s), h("b", { text: s.title }), nameHref(s).startsWith("http")), s.about ? help(s.about) : null)),
    h("td", {}, h("div", { class: "stack-status" }, stackStatus(s), notes.length ? help(notes.join(" ")) : null)),
    // The section heading above the rows names the type, so no Type column.
    // On JFrog OIDC the columns are Type and Location, as for workloads: the trust is a JFrog
    // integration (its stack linked), and it has no region.
    OIDC ? h("td", { class: "stack-id", title: s.id }, link(s.links?.source, "JFrog OIDC trust", true))
      : h("td", { class: "stack-id" }, link(s.links?.source, s.id, true)),
    OIDC ? null : h("td", {}, link(runsIn(s) === "ADO pipeline" ? s.links?.pipeline : s.has_runs ? runsHref(s) : null, runsIn(s), runsIn(s) === "ADO pipeline")),
    OIDC ? h("td", { text: "Global" }) : h("td", {}, checkedAt(s) ? (s.check_job ? link(`?job=${s.check_job}`, shortWhen(checkedAt(s))) : shortWhen(checkedAt(s))) : "—"),
    h("td", {}, h("div", { class: "setup-row-actions nowrap" },
      progress,
      plan,
      log,
      // Inputs, Deploy and Destroy behind "⋯"; Destroy asks once more before it plans.
      menu(`Actions for ${s.title}`, [
        { text: "Inputs", onSelect: () => editInputs(s) },
        // An added deployment that stands nowhere can be removed (its saved inputs forgotten).
        s.id.includes("@") ? { text: "Remove", danger: true, confirm: "Remove this deployment?", disabled: s.busy || !!s.plan || !!s.status?.deployed,
          title: s.status?.deployed ? "Destroy it first" : null,
          onSelect: async () => { await api(`/api/stacks/${s.id}/instances`, { method: "DELETE" }); load(); } } : null,
        { text: "Deploy", disabled: s.busy || s.checking || !!blockedDeploy, title: blockedDeploy || null, onSelect: () => run(s, "plan-apply") },
        { text: "Destroy", danger: true, confirm: "Plan destroy?", disabled: s.busy || s.checking || !!blockedDestroy, title: blockedDestroy || null,
          onSelect: () => run(s, "plan-destroy") },
      ]))));
}

// Links: a cell whose value has a page goes there; ADO pages open in a new tab.
const link = (href, content, external = false) => (href
  ? h("a", { href, ...(external ? { target: "_blank", rel: "noopener noreferrer" } : {}) }, content)
  : content);
// A stack's name opens what it provisions in the console: its workloads; a pipeline identity and state
// or a JFrog trust stack opens its runs.
const SERVES = { "gcp-cloud-run": "gcp?type=gcp_run_job", "gcp-cloud-run-service": "gcp?type=gcp_run_service",
  "gcp-cloud-run-function": "gcp?type=gcp_run_function", "gcp-cloud-run-worker-pool": "gcp?type=gcp_run_worker_pool",
  "azure-container-apps": "azure?type=azure_container_app_job", "azure-container-app": "azure?type=azure_container_app",
  "azure-functions-aca": "azure?type=azure_functions_aca", "azure-functions-premium": "azure?type=azure_functions_premium",
  "azure-functions-flex": "azure?type=azure_functions_flex", "azure-web-app": "azure?type=azure_web_app",
  "gcp-app-engine": "gcp?type=gcp_app_engine" };
const runsArea = () => (OIDC ? "oidc" : "infrastructure");
const runsHref = (s) => `/runs?area=${runsArea()}&cloud=${CLOUD_ID[s.group] || ""}&q=${encodeURIComponent(s.title)}`;
// A pipeline identity is the cloud's ADO trust: its name opens the project's service connections.
const adoSettings = (s) => s.links?.source?.replace(/\/_git\/.*$/, "/_settings/adminservices");
const nameHref = (s) => (SERVES[s.id] ? `/serverless/${SERVES[s.id]}` : (s.id.endsWith("-pipeline-identity") && adoSettings(s))
  || (s.has_runs ? runsHref(s) : s.check_job ? `?job=${s.check_job}` : s.links?.source));

let byId = {};

// Each stack's service icon (the sidebar's), and each cloud's logo: constant SVG from shell.js.
const STACK_ICON = { "gcp-pipeline-identity": "boot", "azure-pipeline-identity": "boot", "gcp-cloud-run": "run", "gcp-cloud-run-service": "run", "gcp-cloud-run-worker-pool": "run",
  "gcp-cloud-run-function": "fn", "gcp-app-engine": "web", "azure-container-apps": "aca", "azure-container-app": "aca",
  "azure-functions-aca": "func", "azure-functions-premium": "func", "azure-functions-flex": "func", "azure-web-app": "web",
  "gcp-jfrog-trust": "boot", "azure-jfrog-trust": "boot" };
const CLOUD_ID = { "Google Cloud": "gcp", Azure: "azure" };
function iconEl(key, cls) {
  const span = h("span", { class: cls });
  span.innerHTML = window.consoleNav?.icon(key) || "";  // constant SVG only, never data
  return span;
}

// Filters over the grouped view: Cloud, Type, Status and free text, kept in the address.
// JFrog OIDC is its own mount (/oidc, scope "oidc"): both clouds' trust stacks; /oidc?cloud=<cloud> opens
// it on that cloud. Infrastructure (/setup) never lists them, and this page never lists infrastructure.
const BASE = document.querySelector('meta[name="console-base"]')?.content || "";
const OIDC = BASE === "/oidc";
const STATES = OIDC ? [["on", "Onboarded"], ["off", "Not onboarded"]]
  : [["deployed", "Deployed"], ["not", "Not deployed"], ["attention", "Needs attention"]];
const F = { q: "", cloud: "", kind: "", state: "", view: "" };
// ?stack=<id> (a Create tile): that one stack, deployed or not, its Inputs open once.
let OPEN_STACK = null, CREATE_STACK = null;
{
  const u = new URLSearchParams(location.search);
  for (const k of ["q", "cloud", "kind", "state", "view"]) if (u.get(k)) F[k] = u.get(k);
  OPEN_STACK = u.get("stack");
  CREATE_STACK = u.get("create");
}
// The live page lists only what stands; ?view=archived lists the rest (never deployed, destroyed, gone).
const ARCHIVED = () => F.view === "archived";
let lastData = null;

function saveQuery() {
  const u = new URLSearchParams();
  for (const k of ["q", "cloud", "kind", "state", "view"]) if (F[k]) u.set(k, F[k]);
  history.replaceState(null, "", `${BASE || "/"}${u.toString() ? `?${u}` : ""}`);
}

function drawFilters(kinds) {
  const set = (k) => (v) => { F[k] = v; saveQuery(); draw(); };
  fill($("filters"), window.consoleBlade.bar({ q: F.q, onQuery: (v) => { F.q = v; saveQuery(); draw(false); }, pills: [
    { label: "Cloud", value: F.cloud, options: [["azure", "Azure"], ["gcp", "Google Cloud"]], onChange: set("cloud") },
    { label: "Type", value: F.kind, options: kinds.map((k) => [k, k]), onChange: set("kind") },
    { label: "Status", value: F.state, options: STATES, onChange: set("state") },
  ].filter(Boolean) }));
}

async function load() {
  lastData = await api("/api/stacks");
  draw();
  if (OPEN_STACK && !load.opened && byId[OPEN_STACK]) {
    load.opened = true;
    editInputs(byId[OPEN_STACK]);
  }
  if (CREATE_STACK && !load.opened && byId[CREATE_STACK]) {
    load.opened = true;
    createFlow(CREATE_STACK);
  }
  if (OIDC && !live.started) loadLive().catch(() => {});
  // While a job runs, keep the rows current so Running turns into its result without a reload.
  clearTimeout(load.timer);
  if (lastData.stacks.some((s) => s.busy || s.checking)) load.timer = setTimeout(() => load().catch(() => {}), POLL_MS * 2);
}

function draw(withFilters = true) {
  const data = lastData;
  byId = Object.fromEntries(data.stacks.map((s) => [s.id, s]));
  if (OIDC) return drawOidc(data, withFilters);
  // The environment every stack here deploys to: it picks the saved inputs, the ADO environments
  // <stack>-<env> and the variable groups jfrog-oidc-<cloud>-<env>.
  // Infrastructure (/setup): the cloud is the root, and under it Common (the pipeline identity and state, used by
  // both), Serverless and App Services, each its own table. JFrog OIDC is not infrastructure: it
  // has its own section (/oidc), made only once that cloud's infrastructure exists.
  const ORDER = ["Common", "Serverless", "App Services", "JFrog OIDC"];
  const sectionOf = (s) => (s.id.endsWith("-pipeline-identity") ? "Common" : s.kind);
  $("page-title").textContent = OIDC ? "JFrog OIDC" : "Infrastructure";
  const all = data.stacks.filter((s) => (OIDC ? s.kind === "JFrog OIDC" : s.kind !== "JFrog OIDC"));
  if (withFilters) drawFilters(ORDER.filter((k) => all.some((s) => sectionOf(s) === k)));
  const q = F.q.trim().toLowerCase();
  const shown = all.filter((s) => (!F.cloud || CLOUD_ID[s.group] === F.cloud) && (!F.kind || sectionOf(s) === F.kind)
    && (!F.state || stateOf(s) === F.state)
    && (!q || [s.title, s.id, s.group, sectionOf(s), s.about].some((v) => String(v || "").toLowerCase().includes(q))));
  $("none").hidden = shown.length > 0;
  essentials(data, all);
  $("count").textContent = `Showing ${shown.length} of ${all.length} records.`;
  // One table, Azure portal style, one block per cloud (never mixed): Azure, then Google Cloud.
  const CLOUD_ORDER = ["Azure", "Google Cloud"];
  const rank = (g) => (CLOUD_ORDER.includes(g) ? CLOUD_ORDER.indexOf(g) : CLOUD_ORDER.length);
  const groups = [...new Set(shown.map((s) => s.group))].sort((x, y) => rank(x) - rank(y));
  const cols = OIDC ? ["Name", "Status", "Stack", "Last checked", ""] : ["Name", "Status", "Stack", "Runs in", "Last checked", ""];
  const count = (list) => (list.every((x) => !standing(x)) ? `${list.length} archived`
    : `${list.filter((x) => x.status?.deployed).length} of ${list.length} deployed`);
  const head = (g, list) => h("h2", { class: "cloud-title home-group-name" }, iconEl(CLOUD_ID[g], "home-group-logo"),
    h("a", { href: `${BASE}?cloud=${CLOUD_ID[g]}` }, g), h("small", { text: count(list) }));
  // Under each cloud, CI/CD, Serverless and App Services never mix: each has its own heading and rows.
  const LABEL = { Common: "CI/CD", "JFrog OIDC": "Pipelines" };
  const sections = ORDER.filter((k) => shown.some((s) => sectionOf(s) === k));
  const subHead = (k, list) => h("tr", { class: "home-group home-subgroup" }, h("th", { scope: "colgroup", colspan: String(cols.length) },
    h("span", { class: "home-group-name" }, h("a", { href: `${BASE}?${new URLSearchParams({ ...(list[0] && !OIDC ? { cloud: CLOUD_ID[list[0].group] } : {}), kind: k })}` }, LABEL[k] || k), h("small", { text: count(list) }))));
  // Each cloud is its own block: its heading (logo, name, count) above its own table with its own
  // column headers, so a cloud name never sits under another cloud's header row.
  const table = (rows) => h("div", { class: "table-wrap" }, h("table", { class: `blade-table home-table stack-table cloud-table cols-${cols.length}` },
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", { scope: "col", text: c })))), rows));
  // Live tables list only what stands (deployed, running, or a plan waiting); every other stack (never
  // deployed, destroyed, deleted outside, partly deleted) is in Archived below, where Deploy still starts it.
  const blocks = (list) => {
    const clouds = groups.filter((g) => list.some((s) => s.group === g));
    return h("div", { class: "cloud-tables" }, clouds.map((g) => {
    const inCloud = list.filter((s) => s.group === g);
    const bodies = sections.map((k) => {
      const list = inCloud.filter((s) => sectionOf(s) === k);
      // A section heading only when more than one section is listed (one alone repeats the Type filter).
      return list.length ? h("tbody", {}, sections.length > 1 ? subHead(k, list) : null, list.map((s) => row(s, k))) : null;
    }).filter(Boolean);
    // A cloud heading only when more than one cloud is listed: alone, it repeats the filter.
    // A cloud heading whenever the page lists more than one cloud (alone, it repeats the filter).
    return h("section", { class: "cloud-block" }, groups.length > 1 ? head(g, inCloud) : null, table(bodies));
  }));
  };
  const list = shown.filter((s) => standing(s) !== ARCHIVED() || (OPEN_STACK && s.id === OPEN_STACK && !ARCHIVED()));
  viewChrome(all.filter((s) => !standing(s)).length);
  $("count").textContent = `Showing ${list.length} of ${all.filter((s) => standing(s) !== ARCHIVED()).length} records.`;
  $("none").hidden = list.length > 0;
  fill($("groups"), list.length ? blocks(list) : null);
}

// Live page vs Archived view: the title, the toolbar link between them, and the empty text.
function viewChrome(nGone) {
  const base = OIDC ? "JFrog OIDC" : "Infrastructure";
  $("page-title").textContent = ARCHIVED() ? `${base} · Archived` : base;
  // Gone stacks are listed under Runs › Archived; this page links back only from its archived view.
  const a = $("view-link");
  a.hidden = !ARCHIVED();
  a.textContent = `${base} (${nGone} archived)`.replace(/ \(\d+ archived\)$/, "");
  a.href = BASE;
  $("none").textContent = ARCHIVED() ? "Nothing archived." : (OIDC ? "Nothing onboarded." : "Nothing deployed.");
}


// JFrog OIDC (/oidc) is its own step, after Infrastructure: it never deploys anything there.
// Per cloud, never mixed: Serverless and App Services (each live workload, onboarded for the runtime
// exchange or not) and Pipelines (the build pipelines' own JFrog
// trust, optional: production adds only the runtime exchange to live services).
const live = { started: false, loading: true, workloads: [], kinds: {}, errors: [] };
const LIVE_CLOUDS = ["azure", "gcp"];
// App Services: the web apps (code on a managed web platform), apart from Serverless.
const APP_SERVICE_KINDS = new Set(["azure_web_app", "gcp_app_engine"]);
const sectionOfKind = (k) => (APP_SERVICE_KINDS.has(k) ? "App Services" : "Serverless");
const words = (v) => String(v || "").replaceAll("_", " ");

async function loadLive() {
  live.started = true;
  const [schema, { configs }] = await Promise.all([api("/serverless/api/schema", { abs: true }), api("/serverless/api/configs", { abs: true })]);
  live.kinds = schema.kinds || {};
  const cfgOf = (w) => configs.find((c) => c.kind === w.kind && String(c.workload || "").split(" / ").pop() === w.name);
  draw();
  for (const c of LIVE_CLOUDS) {   // one cloud at a time: they share the CLI lock
    try {
      const inv = await api(`/serverless/api/inventory/${c}`, { abs: true });
      for (const w of inv.workloads || []) {
        if (w.supported && live.kinds[w.kind]) live.workloads.push({ ...w, cloud: c, cfg: cfgOf(w) || null });
      }
      for (const e of inv.errors || []) live.errors.push(`${CLOUD_NAME[c]}: ${e}`);
    } catch (e) { live.errors.push(`${CLOUD_NAME[c]}: ${e.message}`); }
    draw();
  }
  live.loading = false;
  draw();
}

const CLOUD_NAME = { azure: "Azure", gcp: "Google Cloud" };
const go = (href, text, primary) => h("button", { type: "button", class: primary ? "primary" : "secondary", text,
  onclick: () => location.assign(href) });
const pillOf = (tone, text) => h("span", { class: `blade-state ${tone === "ok" ? "ok" : ""}`, "data-tone": tone, text });

function workloadRow(w) {
  const open = w.cfg ? `/serverless/${w.cloud}/w/${encodeURIComponent(w.cfg.name)}` : `/serverless/${w.cloud}/new?kind=${encodeURIComponent(w.kind)}`;
  const icon = window.consoleNav?.section(w.kind)?.icon || "run";
  return h("tr", { "data-state": w.cfg ? "ok" : "idle" },
    h("td", {}, h("div", { class: "stack-name" }, iconEl(icon, "stack-icon"), link(open, h("b", { text: w.name })))),
    h("td", {}, pillOf(w.cfg ? "ok" : "idle", w.cfg ? "Onboarded" : "Not onboarded")),
    h("td", {}, link(`/serverless/${w.cloud}?type=${encodeURIComponent(w.kind)}`, live.kinds[w.kind]?.label || w.kind)),
    h("td", { text: w.region || "—" }),
    h("td", {}, h("div", { class: "setup-row-actions nowrap" }, go(open, w.cfg ? "Open" : "Onboard", !w.cfg),
      w.cfg ? h("button", { type: "button", class: "secondary", text: "Prove", onclick: (ev) => proveWorkload(w, ev.currentTarget) }) : null)));
}

// Prove: a prove-only run on the workload's own config (the run page holds the approval and the evidence).
async function proveWorkload(w, btn) {
  btn.disabled = true;
  const name = encodeURIComponent(w.cfg.name);
  try {
    const { config } = await api(`/serverless/api/configs/${name}`, { abs: true });
    const res = await api("/serverless/api/runs", { abs: true, method: "POST", body: { config, steps: ["prove"] } });
    location.assign(`/serverless/${w.cloud}/w/${name}/runs/${encodeURIComponent(res.run_id)}`);
  } catch (e) {
    btn.disabled = false;
    btn.title = e.message;
    btn.textContent = "Prove failed";
  }
}

function drawOidc(data, withFilters) {
  const SECTIONS = ["Serverless", "App Services", "Pipelines"];
  $("page-title").textContent = "JFrog OIDC";
  const trust = data.stacks.filter((s) => s.kind === "JFrog OIDC");
  // Every row as {cloud, section, on, text, el}: one list, so filters and counts treat them alike.
  const rows = [
    ...live.workloads.map((w) => ({ cloud: w.cloud, section: sectionOfKind(w.kind), on: !!w.cfg,
      text: [w.name, w.region, live.kinds[w.kind]?.label], el: () => workloadRow(w) })),
    ...trust.map((s) => ({ cloud: CLOUD_ID[s.group], section: "Pipelines", on: !!s.status?.deployed, gone: !standing(s),
      text: [s.title, s.id, s.about], el: () => row(s, "Pipelines") })),
  ];
  if (withFilters) drawFilters(SECTIONS);
  const q = F.q.trim().toLowerCase();
  const shown = rows.filter((r) => (!F.cloud || r.cloud === F.cloud) && (!F.kind || r.section === F.kind)
    && (!F.state || (F.state === "on") === r.on) && (!q || r.text.some((v) => String(v || "").toLowerCase().includes(q))));
  const mine = rows.filter((r) => !F.cloud || r.cloud === F.cloud);
  essentials(data, trust);
  fill($("ess-left"), ...[["Environment", data.env], ["Onboarded", `${mine.filter((r) => r.on).length} of ${mine.length}`]]
    .map(([l, v]) => h("div", { class: "ess-pair" }, h("dt", { text: l }), h("dd", { text: v }))));
  fill($("ess-right"), ...[["Serverless", mine.filter((r) => r.section === "Serverless").length],
    ["App Services", mine.filter((r) => r.section === "App Services").length], ["Pipelines", mine.filter((r) => r.section === "Pipelines").length]]
    .map(([l, v]) => h("div", { class: "ess-pair" }, h("dt", { text: l }), h("dd", { text: String(v) }))));
  $("count").textContent = live.loading && !live.workloads.length ? "Reading the clouds…" : `Showing ${shown.length} of ${rows.length} records.`;
  $("none").hidden = shown.length > 0 || live.loading;
  const cols = ["Name", "Status", "Type", "Location", ""];
  const clouds = ["azure", "gcp"].filter((c) => shown.some((r) => r.cloud === c));
  const sections = SECTIONS.filter((k) => shown.some((r) => r.section === k));
  const subHead = (k, list) => h("tr", { class: "home-group home-subgroup" }, h("th", { scope: "colgroup", colspan: String(cols.length) },
    h("span", { class: "home-group-name" }, h("a", { href: `/oidc?${new URLSearchParams({ cloud: list[0].cloud, kind: k })}`, text: k }),
      h("small", { text: `${list.filter((r) => r.on).length} of ${list.length} onboarded` }))));
  // Workloads are listed from live cloud reads, so they exist; a trust stack with nothing
  // standing goes to Archived below, where Deploy still starts it.
  const blocks = (list) => h("div", { class: "cloud-tables" }, clouds.filter((c) => list.some((r) => r.cloud === c)).map((c) => {
    const inCloud = list.filter((r) => r.cloud === c);
    const bodies = sections.map((k) => {
      const list = inCloud.filter((r) => r.section === k);
      return list.length ? h("tbody", {}, sections.length > 1 ? subHead(k, list) : null, list.map((r) => r.el())) : null;
    }).filter(Boolean);
    return h("section", { class: "cloud-block" },
      clouds.length > 1 ? h("h2", { class: "cloud-title home-group-name" }, iconEl(c, "home-group-logo"),
        h("a", { href: `/oidc?cloud=${c}` }, CLOUD_NAME[c]), h("small", { text: `${inCloud.filter((r) => r.on).length} of ${inCloud.length} onboarded` })) : null,
      h("div", { class: "table-wrap" }, h("table", { class: `blade-table home-table stack-table cloud-table cols-${cols.length}` },
        h("thead", {}, h("tr", {}, cols.map((t) => h("th", { scope: "col", text: t })))), bodies)));
  }));
  const list = shown.filter((r) => !!r.gone === ARCHIVED());
  viewChrome(rows.filter((r) => r.gone).length);
  if (!(live.loading && !live.workloads.length)) $("count").textContent = `Showing ${list.length} of ${rows.filter((r) => !!r.gone === ARCHIVED()).length} records.`;
  $("none").hidden = list.length > 0 || live.loading;
  fill($("groups"), list.length ? blocks(list) : null);
  if (live.errors.length) $("count").append(" ", help(live.errors.join(" ")));
}

// Where a stack runs: pipeline identity stacks and JFrog trust from this page; workload stacks as
// their own stack-setup pipeline in ADO.
const runsIn = (s) => (s.id.endsWith("-pipeline-identity") || s.kind === "JFrog OIDC" ? "This console" : "ADO pipeline");
const checkedAt = (s) => (s.status?.checked ? new Date(s.status.checked * 1000) : null);
const shortWhen = (d) => d ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// Essentials: label : value in two columns, for the cloud on screen (or both).
function essentials(data, all) {
  const mine = all.filter((s) => !F.cloud || CLOUD_ID[s.group] === F.cloud);
  const clouds = [...new Set(mine.map((s) => s.group))];
  const one = clouds.length === 1 ? clouds[0] : null;
  const { deployed, attention, busy } = counts(mine);
  const last = mine.map(checkedAt).filter(Boolean).sort((x, y) => y - x)[0];
  const found = mine.find((s) => s.id.endsWith("-pipeline-identity"));
  const store = (() => {
    const t = found?.status?.outputs?.azure_tfstate;
    if (t && typeof t === "object") return `${t.storage_account} / ${t.container}`;
    return found?.status?.deployed ? "Pipeline identity state store" : "—";
  })();
  fill($("page-logo"), one ? iconEl(CLOUD_ID[one], "") : null);
  if (!one) $("page-logo").innerHTML = OIDC ? (window.consoleNav?.icon("boot") || "") : '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>';
  const pair = (label, value, helpText) => [h("div", { class: "ess-pair" }, h("dt", { text: label }), h("dd", {}, value, helpText ? help(helpText) : null))];
  fill($("ess-left"),
    pair("Environment", data.env, `Stacks deploy to the ${data.env} environment: saved inputs, ADO environments <stack>-${data.env} and variable groups jfrog-oidc-<cloud>-${data.env}.`),
    pair("Deployed", String(deployed)));
  fill($("ess-right"),
    pair("Needs attention", String(attention)),
    pair("In progress", String(busy)),
    OIDC ? pair("Last checked", shortWhen(last)) : pair("State store", store),
    OIDC ? null : pair("Last checked", shortWhen(last)));
}

// Refresh: every stack's state read again (not the last known one), done when the reads finish.
refreshing($("refresh"), async () => {
  await api("/api/refresh", { method: "POST" });
  for (let i = 0; i < 90; i++) {
    await load();
    if (!lastData.stacks.some((s) => s.checking)) return;
    await new Promise((r) => setTimeout(r, 2000));
  }
});
document.querySelector('a.blade-cmd[href^="/runs"]')?.setAttribute("href", `/runs?area=${runsArea()}`);
$("ess-h").onclick = () => {
  const open = $("ess-h").getAttribute("aria-expanded") !== "true";
  $("ess-h").setAttribute("aria-expanded", String(open));
  $("ess-body").hidden = !open;
};

$("flyout-close").onclick = closeFlyout;
$("auth-form").onsubmit = (e) => {
  e.preventDefault();
  const m = $("auth-input").value.match(/#t=([A-Za-z0-9_-]+)/);
  if (m) { setToken(m[1]); $("auth-banner").hidden = true; load(); }
};
// ?job=<id> (from Runs): that run's log, followed while it runs, read-only once finished.
async function openRun(id) {
  const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
  const stack = byId[job.stack];
  if (!stack) return;
  const panel = flyout(`${stack.group} · ${stack.title}: ${jobTitle(job)}`);
  if (job.state === "running") return follow(stack, job, panel);
  fill(panel.status, job.state === "failed" ? statusPill("failed", "Failed") : statusPill("succeeded", "Done"));
  fill(panel.body, stagesView(job), job.state === "failed" ? h("pre", { class: "pf-log" }, job.error || "") : null,
    logBox(job));
}
const RUN_ID = new URLSearchParams(location.search).get("job");

if (!hasToken()) $("auth-banner").hidden = false;
else load().then(() => RUN_ID && openRun(RUN_ID)).catch((e) => { if (e.status === 401) $("auth-banner").hidden = false; });
