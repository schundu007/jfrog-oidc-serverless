// Setup: deploy and destroy test infrastructure. Check reads the stack's outputs; Deploy and Destroy
// plan first, show the plan, and run it only after Approve (destroy also needs the stack name typed).
import { $, api, h, hasToken, setToken, statusPill, flyout, closeFlyout, armed, help } from "/serverless/static/core.js";
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
  if (!s.status) return statusPill("notrun", s.needs?.length ? "Not deployed" : "Checking…");
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
  const box = logBox(job);
  fill(panel.body, h("p", { class: "muted", text: RUNNING[job.action] || "Running…" }), box);
  fill(panel.status, statusPill("running", "Running"));
  const done = await waitJob(job.id, (j) => box.update(j));
  load();
  if (done.state === "failed") {
    fill(panel.status, statusPill("failed", "Failed"));
    return fill(panel.body, nextView(stack, done.next), h("pre", { class: "pf-log" }, done.error || ""), logBox(done));
  }
  if (done.result?.forgotten) return fill(panel.body, h("p", {}, h("b", { text: "State forgotten." }),
    " The stack shows as not deployed; the old state file is kept aside. Deploy builds it again from nothing."), logBox(done, false));
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
  return h("div", { class: "setup-next" }, h("p", {}, h("b", { text: "Next: " }), next.cause),
    h("div", { class: "actions setup-actions" }, nextActions(s, next)));
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
  const destroy = h("button", { type: "button", class: "secondary", text: "Destroy", disabled: s.busy || !!blockedDestroy, title: blockedDestroy });
  destroy.onclick = () => { if (armed(destroy, "Plan destroy?")) run(s, "plan-destroy"); };
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
  return h("tr", { id: `stack-${s.id}` },
    h("td", { title: s.dir }, h("div", { class: "stack-name" }, iconEl(STACK_ICON[s.id], "stack-icon"),
      h("b", { text: s.title }))),
    h("td", {}, h("div", { class: "stack-status" }, stackStatus(s), notes.length ? help(notes.join(" ")) : null)),
    h("td", {}, h("div", { class: "setup-row-actions" },
      progress,
      plan,
      log,
      h("button", { type: "button", class: "secondary", text: "Inputs", onclick: () => editInputs(s) }),
      h("button", { type: "button", class: "primary", text: "Deploy", disabled: s.busy || !!blockedDeploy, title: blockedDeploy,
        onclick: () => run(s, "plan-apply") }),
      destroy)));
}

let byId = {};

// Each stack's service icon (the sidebar's), and each cloud's logo: constant SVG from shell.js.
const STACK_ICON = { "gcp-foundation": "boot", "azure-foundation": "boot", "gcp-cloud-run": "run", "gcp-cloud-run-service": "run", "gcp-cloud-run-worker-pool": "run",
  "gcp-cloud-run-function": "fn", "gke-cluster": "gke", "azure-container-apps": "aca", "azure-container-app": "aca",
  "azure-functions-aca": "func", "azure-functions-premium": "func", "azure-functions-flex": "func", "aks-cluster": "aks" };
const CLOUD_ID = { "Google Cloud": "gcp", Azure: "azure" };
function iconEl(key, cls) {
  const span = h("span", { class: cls });
  span.innerHTML = window.consoleNav?.icon(key) || "";  // constant SVG only, never data
  return span;
}

async function load() {
  const data = await api("/api/stacks");
  byId = Object.fromEntries(data.stacks.map((s) => [s.id, s]));
  $("env").textContent = data.env;
  // Serverless and Kubernetes are separate sections, each with its own card per cloud: the two
  // kinds never share a card or a table, as they never share state, pipelines or dependencies.
  const kinds = {};
  for (const s of data.stacks) ((kinds[s.kind] ||= {})[s.group] ||= []).push(s);
  fill($("groups"), Object.entries(kinds).map(([k, clouds]) => h("section", { class: "setup-kind", "data-kind": k },
    h("h2", { class: "setup-kind-title", text: k }),
    ...Object.entries(clouds).map(([g, list]) => h("section", { class: "card cloud-card", "data-cloud": CLOUD_ID[g] || "" },
      h("div", { class: "card-head cloud-head" }, iconEl(CLOUD_ID[g], "cloud-badge"),
        h("div", { class: "cloud-title" }, h("h3", { text: g }), h("span", { class: "cloud-sub",
          text: `${list.length} stack${list.length === 1 ? "" : "s"} · ${list.filter((x) => x.status?.deployed).length} deployed` }))),
      h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, list.map(row)))))))));
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
