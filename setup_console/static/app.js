// Setup: deploy and destroy test infrastructure. Check reads the stack's outputs; Deploy and Destroy
// plan first, show the plan, and run it only after Approve (destroy also needs the stack name typed).
import { $, api, h, hasToken, setToken, statusPill, flyout, closeFlyout, help, menu, listEditor, mapEditor, markKnown } from "/serverless/static/core.js";
import { logBox as tableLog } from "/serverless/static/logview.js";

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

const RUNNING = { status: "Reading the stack's Terraform state…", "plan-apply": "Planning the deploy…",
  "plan-destroy": "Planning the destroy…", apply: "Applying the approved plan…" };

function stackStatus(s) {
  if (s.missing?.length) return statusPill("notrun", "Inputs needed");
  if (s.busy) return statusPill("running", "Running");
  if (s.plan) return statusPill("waiting_approval", s.plan.action === "destroy" ? "Destroy planned" : "Deploy planned");
  // Its state lives in the foundation: with the foundation missing, it cannot be deployed.
  // Waiting on another stack (its foundation): say which.
  if (s.needs?.length && !s.status?.deployed) return statusPill("waiting_approval", `Needs ${titleOf(s.needs[0])}`);
  if (!s.status) return statusPill("notrun", "Checking…");
  if (s.status.deployed) return statusPill("succeeded", "Deployed");
  // Deleted outside the console, validated with the cloud: the evidence is behind the row's "?".
  if (s.status.deleted) {
    return partlyDeleted(s) ? statusPill("waiting_approval", "Partly deleted") : statusPill("notrun", "Deleted");
  }
  // A status read that failed (e.g. no state bucket yet) also means nothing is deployed; the reason is behind the "?".
  return statusPill("notrun", "Not deployed");
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
  if (result?.destroyed) return h("p", {}, h("b", { text: "Destroyed" }), help("Nothing of this stack is left in its Terraform state."));
  if (!result?.deployed && !result?.applied) {
    return h("p", {}, h("b", { text: "Not deployed" }), help(`No Terraform state yet. Deploy creates: ${stack.about}`));
  }
  return [h("p", {}, h("b", { text: "Deployed" }), help(stack.next ? `${stack.about} Next: ${stack.next}` : stack.about)),
    outputsTable(result.outputs)];
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
  const box = logBox(job);
  fill(panel.body, h("p", { class: "muted", text: RUNNING[job.action] || "Running…" }), box);
  fill(panel.status, statusPill("running", "Running"));
  const done = await waitJob(job.id, (j) => box.update(j));
  load();
  if (done.state === "failed") {
    fill(panel.status, statusPill("failed", "Failed"));
    return fill(panel.body, nextView(stack, done.next), h("pre", { class: "pf-log" }, done.error || ""), logBox(done));
  }
  if (done.result?.forgotten) return fill(panel.body, h("p", {}, h("b", { text: "State forgotten" }),
    help("The stack shows as not deployed; the old state file is kept aside. Deploy builds it again from nothing.")), logBox(done, false));
  fill(panel.status, statusPill("succeeded", "Done"));
  // Validated as deleted outside the console: nothing to plan or approve.
  if (done.result?.already_deleted) return fill(panel.body, h("p", {}, h("b", { text: done.result.message })), logBox(done, false));
  if (done.action.startsWith("plan-")) return fill(panel.body, planView(stack, done.result, panel), logBox(done, false));
  fill(panel.body, resultView(stack, done.result), logBox(done, false));
}

async function run(stack, action, approval) {
  const verb = { status: "Check", "plan-apply": "Deploy", "plan-destroy": "Destroy", forget: "Forget state" }[action];
  const panel = flyout(`${stack.group} · ${stack.title}: ${verb}`);
  try { await follow(stack, await api(`/api/stacks/${stack.id}/jobs`, { method: "POST", body: { action, approval } }), panel); }
  catch (e) { fill(panel.status, statusPill("failed", e.message)); }
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
  const el = h("input", { id, type: f.kind === "number" ? "number" : "text", spellcheck: "false",
    value: f.current ?? "", required: f.required });
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

async function editInputs(stack) {
  const panel = flyout(`${stack.group} · ${stack.title}: Inputs`);
  const draw = (form) => {
    // A list, map or object is a group of inputs: a div, so its rows' buttons are not the label's target.
    const fields = form.fields.filter((f) => f.kind !== "json").map((f) => h(["list", "map", "object"].includes(f.kind) ? "div" : "label", { class: "field" },
      h("span", {}, h("span", { class: "field-label", text: f.name }), f.required ? h("span", { class: "small", text: "required" }) : null,
        f.description ? help(f.description) : null),
      input(f)));
    const save = h("button", { type: "submit", class: "primary", text: "Save" });
    const el = h("form", { class: "setup-form", autocomplete: "off" }, fields, h("div", { class: "actions setup-actions" }, save));
    el.onsubmit = async (e) => {
      e.preventDefault();
      save.disabled = true;
      try {
        const values = Object.fromEntries(form.fields.filter((f) => f.kind !== "json").map((f) => [f.name, readField(f)]));
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

const titleOf = (id) => byId[id] ? byId[id].title : id;
const ACTION_NAME = { status: "State read", "plan-apply": "Deploy plan", "plan-destroy": "Destroy plan", apply: "Apply", forget: "Forget state" };

// Terraform's error, cut to its point: no "terraform x failed (exit 1): Error:" prefix, one short line.
function brief(text, max = 140) {
  const t = String(text || "").replace(/^terraform \S+ failed \(exit \d+\):\s*/, "").replace(/^Error:\s*/, "").split("\n")[0];
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

// What stops or failed this stack, each with where to go: the foundation it needs, the stacks that
// stand on it, the last run that failed. A failed state read is left out when the missing foundation
// already explains it.
const partlyDeleted = (s) => Boolean(s.status?.deleted && Object.keys(s.status.outputs || {}).length);

// What stops or failed this stack, as plain sentences: shown only behind the circled "?" next to its
// status, never as a banner. A failed state read is left out when the missing foundation explains it.
function warnings(s) {
  const out = [];
  if (s.status?.deleted && !s.busy && !s.plan) out.push(s.status.deleted);
  else if (s.status?.error && !s.needs?.length) out.push(s.status.error);
  if (s.needs?.length) out.push(`Deploy ${s.needs.map(titleOf).join(", ")} first.`);
  if (s.dependents?.length) out.push(`Destroy ${s.dependents.map(titleOf).join(", ")} first.`);
  const failed = lastFailure(s);
  if (failed) out.push(`${ACTION_NAME[failed.action] || "Last run"} failed: ${brief(failed.error) || "see the log"}`);
  return out;
}
const lastFailure = (s) => (s.failed && !s.busy && !(s.failed.action === "status" && s.needs?.length) ? s.failed : null);

function row(s) {
  const blockedDestroy = s.dependents?.length ? `Destroy ${s.dependents.map(titleOf).join(", ")} first` : "";
  const plan = s.plan ? h("button", { type: "button", class: "primary", text: "Review plan" }) : null;
  if (plan) plan.onclick = () => { const p = flyout(`${s.group} · ${s.title}`); fill(p.body, planView(s, s.plan, p)); };
  // A running job keeps going when its drawer is closed or the page reloads: this reopens its live log.
  const progress = s.job ? h("button", { type: "button", class: "primary", text: "Progress" }) : null;
  if (progress) {
    const verb = { status: "Check", "plan-apply": "Deploy", "plan-destroy": "Destroy", apply: "Apply" }[s.job.action];
    progress.onclick = () => follow(s, s.job, flyout(`${s.group} · ${s.title}: ${verb}`));
  }
  const blockedDeploy = s.needs?.length ? `Deploy ${s.needs.map(titleOf).join(", ")} first` : "";
  const notes = warnings(s);
  // A failed run keeps its log one click away; what to do next is in that log's drawer.
  const failed = lastFailure(s);
  const log = failed ? h("button", { type: "button", class: "secondary", text: "Log",
    onclick: () => follow(s, failed, flyout(`${s.group} · ${s.title}: ${ACTION_NAME[failed.action] || failed.action}`)) }) : null;
  // The row's state tones its status (green deployed, dark needs you, grey idle); a row waiting on another stack says which.
  const state = s.busy || s.plan ? "busy" : s.status?.deployed ? "ok" : partlyDeleted(s) || lastFailure(s) || s.needs?.length || s.missing?.length ? "attention" : "idle";
  return h("tr", { id: `stack-${s.id}`, "data-state": state, class: s.needs?.length ? "blocked" : null },
    h("td", { title: s.dir }, h("div", { class: "stack-name" }, iconEl(STACK_ICON[s.id], "stack-icon"),
      h("b", { text: s.title }), s.about ? help(s.about) : null)),
    h("td", {}, h("div", { class: "stack-status" }, stackStatus(s), notes.length ? help(notes.join(" ")) : null)),
    h("td", {}, h("div", { class: "setup-row-actions" },
      progress,
      plan,
      log,
      // Inputs, Deploy and Destroy behind "⋯"; Destroy asks once more before it plans.
      menu(`Actions for ${s.title}`, [
        { text: "Inputs", onSelect: () => editInputs(s) },
        { text: "Deploy", disabled: s.busy || !!blockedDeploy, title: blockedDeploy || null, onSelect: () => run(s, "plan-apply") },
        { text: "Destroy", danger: true, confirm: "Plan destroy?", disabled: s.busy || !!blockedDestroy, title: blockedDestroy || null,
          onSelect: () => run(s, "plan-destroy") },
      ]))));
}

let byId = {};

// Each stack's service icon (the sidebar's), and each cloud's logo: constant SVG from shell.js.
const STACK_ICON = { "gcp-foundation": "boot", "azure-foundation": "boot", "gcp-cloud-run": "run", "gcp-cloud-run-service": "run", "gcp-cloud-run-worker-pool": "run",
  "gcp-cloud-run-function": "fn", "gke-cluster": "gke", "azure-container-apps": "aca", "azure-container-app": "aca",
  "azure-functions-aca": "func", "azure-functions-premium": "func", "aks-cluster": "aks",
  "gcp-jfrog-trust": "boot", "azure-jfrog-trust": "boot" };
const CLOUD_ID = { "Google Cloud": "gcp", Azure: "azure" };
function iconEl(key, cls) {
  const span = h("span", { class: cls });
  span.innerHTML = window.consoleNav?.icon(key) || "";  // constant SVG only, never data
  return span;
}

async function load() {
  const data = await api("/api/stacks");
  byId = Object.fromEntries(data.stacks.map((s) => [s.id, s]));
  // The environment every stack here deploys to: it picks the saved inputs, the ADO environments
  // <stack>-<env> and the variable groups jfrog-oidc-<cloud>-<env>.
  fill($("env"), `Environment: ${data.env}`, help(`Stacks deploy to the ${data.env} environment: saved inputs, ADO environments <stack>-${data.env} and variable groups jfrog-oidc-<cloud>-${data.env}.`));
  // The cloud is the root: one column per cloud, and under it Common (the CI/CD bootstrap, used by
  // both kinds), Serverless and Kubernetes, each its own table, then JFrog OIDC: made only once the
  // infrastructure exists, never by an infrastructure stack. The kinds never share a table.
  const ORDER = ["Common", "Serverless", "Kubernetes", "JFrog OIDC"];
  const sectionOf = (s) => (s.id.endsWith("-foundation") ? "Common" : s.kind);
  const clouds = {};
  for (const s of data.stacks) ((clouds[s.group] ||= {})[sectionOf(s)] ||= []).push(s);
  const done = (list) => list.filter((x) => x.status?.deployed).length;
  // A thin meter of deployed / total: the count at a glance, no extra words.
  const meter = (n, of) => {
    const fillEl = h("span", { class: "meter-fill" });
    fillEl.style.width = `${of ? Math.round((100 * n) / of) : 0}%`;   // CSSOM, allowed by the CSP
    return h("span", { class: "meter", role: "img", "aria-label": `${n} of ${of} deployed`, title: `${n} of ${of} deployed` }, fillEl);
  };
  // Clouds stacked top to bottom: Azure first, then Google Cloud.
  const CLOUD_ORDER = ["Azure", "Google Cloud"];
  const rank = (g) => (CLOUD_ORDER.includes(g) ? CLOUD_ORDER.indexOf(g) : CLOUD_ORDER.length);
  fill($("groups"), Object.entries(clouds).sort(([a], [b]) => rank(a) - rank(b)).map(([g, kinds]) => {
    const all = Object.values(kinds).flat();
    return h("section", { class: "cloud-root", "data-cloud": CLOUD_ID[g] || "" },
      h("div", { class: "cloud-head" }, iconEl(CLOUD_ID[g], "cloud-badge"),
        h("div", { class: "cloud-title" }, h("h3", { text: g }), h("span", { class: "cloud-sub", text: `${done(all)} of ${all.length} deployed` })),
        meter(done(all), all.length)),
      ...ORDER.filter((k) => kinds[k]).map((k) => h("div", { class: "kind-folder", "data-kind": k },
        h("h4", { class: "kind-folder-title" }, h("span", { text: k }), h("span", { class: "kind-count", text: `${done(kinds[k])}/${kinds[k].length}` })),
        h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, kinds[k].map(row)))))));
  }));
  // While a job runs, keep the rows current so Running turns into its result without a reload.
  clearTimeout(load.timer);
  if (data.stacks.some((s) => s.busy)) load.timer = setTimeout(() => load().catch(() => {}), POLL_MS * 2);
}

$("flyout-close").onclick = closeFlyout;
$("auth-form").onsubmit = (e) => {
  e.preventDefault();
  const m = $("auth-input").value.match(/#t=([A-Za-z0-9_-]+)/);
  if (m) { setToken(m[1]); $("auth-banner").hidden = true; load(); }
};
if (!hasToken()) $("auth-banner").hidden = false;
else load().catch((e) => { if (e.status === 401) $("auth-banner").hidden = false; });
