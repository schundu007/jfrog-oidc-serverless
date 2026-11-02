// Setup: deploy and destroy test infrastructure. Check reads the stack's outputs; Deploy and Destroy
// plan first, show the plan, and run it only after Approve (destroy also needs the stack name typed).
import { $, api, h, hasToken, setToken, statusPill, flyout, closeFlyout, help, menu, listEditor, mapEditor, markKnown } from "/serverless/static/core.js";
import { logBox as tableLog } from "/serverless/static/logview.js";
import { counts, lastFailure, partlyDeleted, standing, stateOf } from "./listing.js";

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

// Status as on Home: a coloured dot and plain words (green deployed, amber needs you or running, grey idle).
const dot = (tone, text) => h("span", { class: `blade-state ${tone}`, text });
function stackStatus(s) {
  if (s.missing?.length) return dot("warn", "Inputs needed");
  if (s.busy) return dot("warn", "Running");
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

// A JFrog OIDC row names the infrastructure it stands on, which this page does not list: its title only.
const titleOf = (id) => byId[id] ? byId[id].title : lastData?.titles?.[id] || id;
const ACTION_NAME = { status: "State read", "plan-apply": "Deploy plan", "plan-destroy": "Destroy plan", apply: "Apply", forget: "Forget state" };

// Terraform's error, cut to its point: no "terraform x failed (exit 1): Error:" prefix, one short line.
function brief(text, max = 140) {
  const t = String(text || "").replace(/^terraform \S+ failed \(exit \d+\):\s*/, "").replace(/^Error:\s*/, "").split("\n")[0];
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

// What stops or failed this stack, each with where to go: the foundation it needs, the stacks that
// stand on it, the last run that failed. A failed state read is left out when the missing foundation
// already explains it.

// What stops or failed this stack, as plain sentences: shown only behind the circled "?" next to its
// status, never as a banner. A failed state read is left out when the missing foundation explains it.
function warnings(s) {
  const out = [];
  if (s.status?.deleted && !s.busy && !s.plan) out.push(s.status.deleted);
  else if (s.status?.error && !s.needs?.length) out.push(s.status.error);
  if (s.needs?.length) out.push(`Deploy ${s.needs.map(titleOf).join(", ")} first.`);
  if (s.dependents?.length) out.push(`Destroy ${s.dependents.map(titleOf).join(", ")} first.`);
  else if (s.unchecked?.length) out.push(`Destroy waits until ${s.unchecked.map(titleOf).join(", ")} ${s.unchecked.length > 1 ? "are" : "is"} checked.`);
  const failed = lastFailure(s);
  if (failed) out.push(`${ACTION_NAME[failed.action] || "Last run"} failed: ${brief(failed.error) || "see the log"}`);
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
      link(nameHref(s), h("b", { text: s.title })), s.about ? help(s.about) : null)),
    h("td", {}, h("div", { class: "stack-status" }, stackStatus(s), notes.length ? help(notes.join(" ")) : null)),
    // The section heading above the rows names the type, so no Type column.
    // On JFrog OIDC the columns are Type and Location, as for workloads and clusters: the trust is a JFrog
    // integration (its stack linked), and it has no region.
    OIDC ? h("td", { class: "stack-id", title: s.id }, link(s.links?.source, "JFrog OIDC trust", true))
      : h("td", { class: "stack-id" }, link(s.links?.source, s.id, true)),
    OIDC ? null : h("td", {}, link(runsIn(s) === "ADO pipeline" ? s.links?.pipeline : runsHref(s), runsIn(s), runsIn(s) === "ADO pipeline")),
    OIDC ? h("td", { text: "Global" }) : h("td", {}, checkedAt(s) ? link(runsHref(s), shortWhen(checkedAt(s))) : "—"),
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

// Links: a cell whose value has a page goes there; ADO pages open in a new tab.
const link = (href, content, external = false) => (href
  ? h("a", { href, ...(external ? { target: "_blank", rel: "noopener noreferrer" } : {}) }, content)
  : content);
// A stack's name opens what it provisions in the console: its workloads or clusters; a CI/CD bootstrap
// or a JFrog trust stack opens its runs.
const SERVES = { "gcp-cloud-run": "gcp?type=gcp_run_job", "gcp-cloud-run-service": "gcp?type=gcp_run_service",
  "gcp-cloud-run-function": "gcp?type=gcp_run_function", "gcp-cloud-run-worker-pool": "gcp?type=gcp_run_worker_pool",
  "azure-container-apps": "azure?type=azure_container_app_job", "azure-container-app": "azure?type=azure_container_app",
  "azure-functions-aca": "azure?type=azure_functions_aca", "azure-functions-premium": "azure?type=azure_functions_premium",
  "azure-functions-flex": "azure?type=azure_functions_flex" };
const CLUSTERS = { "aks-cluster": "/k8s/clusters/aks", "gke-cluster": "/k8s/clusters/gke" };
const runsArea = () => (OIDC ? "oidc" : "infrastructure");
const runsHref = (s) => `/runs?area=${runsArea()}&cloud=${CLOUD_ID[s.group] || ""}&q=${encodeURIComponent(s.title)}`;
const nameHref = (s) => (SERVES[s.id] ? `/serverless/${SERVES[s.id]}` : CLUSTERS[s.id] || runsHref(s));

let byId = {};

// Each stack's service icon (the sidebar's), and each cloud's logo: constant SVG from shell.js.
const STACK_ICON = { "gcp-foundation": "boot", "azure-foundation": "boot", "gcp-cloud-run": "run", "gcp-cloud-run-service": "run", "gcp-cloud-run-worker-pool": "run",
  "gcp-cloud-run-function": "fn", "gke-cluster": "gke", "azure-container-apps": "aca", "azure-container-app": "aca",
  "azure-functions-aca": "func", "azure-functions-premium": "func", "azure-functions-flex": "func", "aks-cluster": "aks", "aks-jfrog-trust": "aks",
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
{
  const u = new URLSearchParams(location.search);
  for (const k of ["q", "cloud", "kind", "state", "view"]) if (u.get(k)) F[k] = u.get(k);
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
  if (OIDC && !live.started) loadLive().catch(() => {});
  // While a job runs, keep the rows current so Running turns into its result without a reload.
  clearTimeout(load.timer);
  if (lastData.stacks.some((s) => s.busy)) load.timer = setTimeout(() => load().catch(() => {}), POLL_MS * 2);
}

function draw(withFilters = true) {
  const data = lastData;
  byId = Object.fromEntries(data.stacks.map((s) => [s.id, s]));
  if (OIDC) return drawOidc(data, withFilters);
  // The environment every stack here deploys to: it picks the saved inputs, the ADO environments
  // <stack>-<env> and the variable groups jfrog-oidc-<cloud>-<env>.
  // Infrastructure (/setup): the cloud is the root, and under it Common (the CI/CD bootstrap, used by
  // both kinds), Serverless and Kubernetes, each its own table. JFrog OIDC is not infrastructure: it
  // has its own section (/oidc), made only once that cloud's infrastructure exists.
  const ORDER = ["Common", "Serverless", "Kubernetes", "JFrog OIDC"];
  // On JFrog OIDC, a cluster's trust is its own Kubernetes section, never mixed with the pipelines' trust.
  const sectionOf = (s) => (s.id.endsWith("-foundation") ? "Common" : (OIDC && s.section) || s.kind);
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
  // Under each cloud, CI/CD, Serverless and Kubernetes never mix: each has its own heading and rows.
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
  const list = shown.filter((s) => standing(s) !== ARCHIVED());
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
// Per cloud, never mixed: Serverless (each live workload, onboarded for the runtime exchange or not),
// Kubernetes (each cluster's kubelet credential provider) and Pipelines (the build pipelines' own JFrog
// trust, optional: production adds only the runtime exchange to live services).
const live = { started: false, loading: true, workloads: [], clusters: [], kinds: {}, errors: [] };
const LIVE_CLOUDS = ["azure", "gcp"];
const CLUSTER_CLOUD = { aks: "azure", gke: "gcp" };
const CLUSTER_STATE = { working: ["ok", "Onboarded"], broken: ["attention", "Not working"] };
const words = (v) => String(v || "").replaceAll("_", " ");

async function loadLive() {
  live.started = true;
  const [schema, { configs }] = await Promise.all([api("/serverless/api/schema", { abs: true }), api("/serverless/api/configs", { abs: true })]);
  live.kinds = schema.kinds || {};
  const cfgOf = (w) => configs.find((c) => c.kind === w.kind && String(c.workload || "").split(" / ").pop() === w.name);
  try {
    const fleet = await api("/k8s/api/fleet", { abs: true });
    live.clusters = (fleet.clusters || []).map((c) => ({ ...c, platform: c.cloud || c.platform || "aks" }));
  } catch (e) { live.errors.push(`Kubernetes: ${e.message}`); }
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

function clusterRow(c) {
  const href = `/k8s/clusters/${c.platform}/${encodeURIComponent(c.name)}`;
  const [tone, text] = CLUSTER_STATE[c.state] || ["idle", words(c.state) || "Not onboarded"];
  return h("tr", { "data-state": tone },
    h("td", {}, h("div", { class: "stack-name" }, iconEl(c.platform, "stack-icon"), link(href, h("b", { text: c.name })))),
    h("td", {}, pillOf(tone, text), c.problem ? help(c.problem) : null),
    h("td", {}, link(`/k8s/clusters/${c.platform}`, c.platform === "gke" ? "GKE cluster" : "AKS cluster")),
    h("td", { text: c.location || c.cluster || "—" }),
    h("td", {}, h("div", { class: "setup-row-actions nowrap" }, go(href, tone === "ok" ? "Open" : "Onboard", tone !== "ok"),
      tone === "ok" ? go(`${href}?tab=proof`, "Prove") : null)));
}

function drawOidc(data, withFilters) {
  const SECTIONS = ["Serverless", "Kubernetes", "Pipelines"];
  $("page-title").textContent = "JFrog OIDC";
  const trust = data.stacks.filter((s) => s.kind === "JFrog OIDC");
  // Every row as {cloud, section, on, text, el}: one list, so filters and counts treat them alike.
  const rows = [
    ...live.workloads.map((w) => ({ cloud: w.cloud, section: "Serverless", on: !!w.cfg,
      text: [w.name, w.region, live.kinds[w.kind]?.label], el: () => workloadRow(w) })),
    ...live.clusters.map((c) => ({ cloud: CLUSTER_CLOUD[c.platform], section: "Kubernetes", on: c.state === "working",
      text: [c.name, c.platform, c.location], el: () => clusterRow(c) })),
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
    ["Kubernetes", mine.filter((r) => r.section === "Kubernetes").length], ["Pipelines", mine.filter((r) => r.section === "Pipelines").length]]
    .map(([l, v]) => h("div", { class: "ess-pair" }, h("dt", { text: l }), h("dd", { text: String(v) }))));
  $("count").textContent = live.loading && !live.workloads.length ? "Reading the clouds…" : `Showing ${shown.length} of ${rows.length} records.`;
  $("none").hidden = shown.length > 0 || live.loading;
  const cols = ["Name", "Status", "Type", "Location", ""];
  const clouds = ["azure", "gcp"].filter((c) => shown.some((r) => r.cloud === c));
  const sections = SECTIONS.filter((k) => shown.some((r) => r.section === k));
  const subHead = (k, list) => h("tr", { class: "home-group home-subgroup" }, h("th", { scope: "colgroup", colspan: String(cols.length) },
    h("span", { class: "home-group-name" }, h("a", { href: `/oidc?${new URLSearchParams({ cloud: list[0].cloud, kind: k })}`, text: k }),
      h("small", { text: `${list.filter((r) => r.on).length} of ${list.length} onboarded` }))));
  // Workloads and clusters are listed from live cloud reads, so they exist; a trust stack with nothing
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

// Where a stack runs: foundations and JFrog trust from this page; workload and cluster stacks as
// their own stack-setup pipeline in ADO.
const runsIn = (s) => (s.id.endsWith("-foundation") || s.kind === "JFrog OIDC" ? "This console" : "ADO pipeline");
const checkedAt = (s) => (s.status?.checked ? new Date(s.status.checked * 1000) : null);
const shortWhen = (d) => d ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// Essentials: label : value in two columns, for the cloud on screen (or both).
function essentials(data, all) {
  const mine = all.filter((s) => !F.cloud || CLOUD_ID[s.group] === F.cloud);
  const clouds = [...new Set(mine.map((s) => s.group))];
  const one = clouds.length === 1 ? clouds[0] : null;
  const { deployed, attention, busy } = counts(mine);
  const last = mine.map(checkedAt).filter(Boolean).sort((x, y) => y - x)[0];
  const found = mine.find((s) => s.id.endsWith("-foundation"));
  const store = (() => {
    const t = found?.status?.outputs?.azure_tfstate;
    if (t && typeof t === "object") return `${t.storage_account} / ${t.container}`;
    return found?.status?.deployed ? "Foundation state store" : "—";
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

$("refresh").onclick = () => load().catch(() => {});
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
  const panel = flyout(`${stack.group} · ${stack.title}: ${ACTION_NAME[job.action] || job.action}`);
  if (job.state === "running") return follow(stack, job, panel);
  fill(panel.status, job.state === "failed" ? statusPill("failed", "Failed") : statusPill("succeeded", "Done"));
  fill(panel.body, job.state === "failed" ? h("pre", { class: "pf-log" }, job.error || "") : null,
    job.started ? h("p", { class: "muted", text: `Started ${new Date(job.started * 1000).toLocaleString()}` }) : null, logBox(job));
}
const RUN_ID = new URLSearchParams(location.search).get("job");

if (!hasToken()) $("auth-banner").hidden = false;
else load().then(() => RUN_ID && openRun(RUN_ID)).catch((e) => { if (e.status === 401) $("auth-banner").hidden = false; });
