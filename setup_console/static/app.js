// Setup: deploy and destroy test infrastructure. Check reads the stack's outputs; Deploy and Destroy
// plan first, show the plan, and run it only after Approve (destroy also needs the stack name typed).
import { $, api, h, hasToken, setToken, statusPill, flyout, closeFlyout, armed } from "/serverless/static/core.js";

const POLL_MS = 1500;
const fill = (el, ...kids) => { el.replaceChildren(...kids.flat().filter(Boolean)); return el; };

async function waitJob(id, onTick) {
  for (;;) {
    const j = await api(`/api/jobs/${id}`);
    onTick?.(j);
    if (j.state !== "running") return j;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

function logBox(lines) { return h("pre", { class: "pf-log" }, (lines || []).join("\n")); }

function stackStatus(s) {
  if (s.missing?.length) return statusPill("notrun", "Inputs needed");
  if (s.busy) return statusPill("running", "Running");
  if (s.plan) return statusPill("waiting_approval", s.plan.action === "destroy" ? "Destroy planned" : "Deploy planned");
  if (!s.status) return statusPill("notrun", "Not checked");
  return s.status.deployed ? statusPill("succeeded", "Deployed") : statusPill("notrun", "Not deployed");
}

function outputsTable(outputs) {
  const rows = Object.entries(outputs || {}).filter(([, v]) => v !== null && typeof v !== "object");
  if (!rows.length) return null;
  return h("table", { class: "facts" }, h("tbody", {}, rows.map(([k, v]) => h("tr", {}, h("th", { text: k }), h("td", { text: String(v) })))));
}

function planView(stack, plan, panel) {
  const counts = Object.entries(plan.summary || {}).filter(([k, n]) => n && k !== "no-op" && k !== "read").map(([k, n]) => `${n} ${k}`);
  const typed = plan.destructive?.length ? h("input", { type: "text", placeholder: plan.confirmation_phrase, "aria-label": "Type the stack name", spellcheck: "false" }) : null;
  const approve = h("button", { type: "button", class: "primary", text: plan.action === "destroy" ? "Approve and destroy" : "Approve and deploy" });
  const discard = h("button", { type: "button", class: "secondary", text: "Discard" });
  approve.onclick = async () => {
    approve.disabled = true;
    try {
      const job = await api(`/api/stacks/${stack.id}/jobs`, { method: "POST",
        body: { action: "apply", approval: { plan_sha256: plan.sha256, typed_confirmation: typed ? typed.value : undefined } } });
      await follow(stack, job, panel);
    } catch (e) { approve.disabled = false; fill(panel.status, statusPill("failed", e.message)); }
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
  fill(panel.body, box);
  fill(panel.status, statusPill("running", "Running"));
  const done = await waitJob(job.id, (j) => { box.textContent = j.log.join("\n"); });
  load();
  if (done.state === "failed") {
    fill(panel.status, statusPill("failed", "Failed"));
    return fill(panel.body, h("pre", { class: "pf-log" }, done.error || ""), box);
  }
  fill(panel.status, statusPill("succeeded", "Done"));
  if (done.action.startsWith("plan-")) return fill(panel.body, planView(stack, done.result, panel), box);
  fill(panel.body, outputsTable(done.result?.outputs), box);
}

async function run(stack, action) {
  const panel = flyout(`${stack.title}: ${{ status: "Check", "plan-apply": "Deploy", "plan-destroy": "Destroy" }[action]}`);
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
  const panel = flyout(`${stack.title}: Inputs`);
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
        fill(panel.status, statusPill(next.missing.length ? "waiting_approval" : "succeeded",
          next.missing.length ? `Missing: ${next.missing.join(", ")}` : "Saved"));
        load();
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
  if (plan) plan.onclick = () => { const p = flyout(s.title); fill(p.body, planView(s, s.plan, p)); };
  return h("tr", {},
    h("td", {}, h("b", { text: s.title }), h("div", { class: "small mono", text: s.id })),
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
