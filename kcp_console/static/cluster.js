// One cluster at /clusters/<cloud>/<name>: a header (name, state, facts, Verify / Set up and a "⋯" menu),
// an approval banner when a run waits for one, then the tabs Summary · Runs · Configuration (?tab=).
//
// Summary runs each action by itself: dry run, then the plan for one approval, then the real run, then
// the evidence and a fresh status read. The action is rebuilt from the server's run records, so a page
// refresh or another page in between finds it where it was. The approval is the only click inside it.

import { api, h, fill, help, state, refreshConfigs, route, go, url, clusterPath, runPath, markOf, CLOUDS as CLOUD, TABS } from "./app.js";
import { $, muted, brief, icon, checklist, pips, when, duration, moreMenu, confirmed, link, liveSteps, stepStatuses,
  TITLES, ACTIVE, isDry, runTitle, runStarted } from "./ui.js";
import { renderConfig } from "./configure.js";

const live = {};            // config name -> {loading, facts, preflight, proof, error}
const acts = {};            // config name -> the newest action: {which, dry, real, error}
const runsOf = {};          // config name -> its runs, newest first
const boxes = {};           // real run id -> its approval box (kept, so a re-render keeps what was typed)
const causes = {};          // failed run id -> the diagnosed cause
const driving = {};         // config name -> true while this page creates or approves a run
let timer = null;

const ESS_HELP = {
  Subscription: "Azure subscription that holds the cluster.",
  Project: "Google Cloud project that holds the cluster.",
  "Resource group": "Resource group of the AKS cluster.",
  Location: "The cluster's region or zone.",
  Platform: "Where the cluster runs.",
  "Kubernetes version": "Control plane version.",
  Nodes: "Nodes the cluster reports now.",
  "OIDC issuer": "The cluster publishes the signing keys of its service account tokens, so JFrog can verify them.",
  "Workload Identity": "Pods can use cloud identities: the provider needs it to get its identity token.",
  "Credential provider": "Version of the JFrog kubelet credential provider installed by Helm.",
  "JFrog provider": "The OIDC integration in JFrog that trusts this cluster's tokens.",
  "Service account": "The provider's Kubernetes service account, allowed to request tokens for the kubelet.",
  "Mapped user": "JFrog user pulls run as, and how long each JFrog token lives.",
};
const CARD_HELP = {
  Problems: "Checks that fail now. Each one blocks pulls through JFrog or will soon.",
  "OIDC proof": "The last proof: a test pod pulled an image through JFrog with no pull secret, and JFrog logged the mapped user.",
  Essentials: "What the cluster and JFrog report right now, read live with read-only calls.",
  "Set up": "Dry run first, then your approval, then the real change: Helm install, JFrog OIDC provider and mapping.",
  Verify: "Read-only: re-reads the cluster and JFrog and pulls the test image again.",
  "Tear down": "Removes what Set up created on this cluster and in JFrog. Dry run and approval first.",
};

const P = () => $("panel-cluster");
const here = () => { const r = route(); return r.page === "cluster" ? r : null; };

export function initCluster() {
  state.on("page", (r) => {
    if (r.page !== "cluster") return;
    if (!open(r.name)) return;
    render();
    if (!live[r.name]) readStatus(r.name); else refreshAction(r.name);
  });
  // A saved configuration: read the cluster again with it.
  state.on("config", (name) => { const r = here(); if (r && r.name === name && live[name]) readStatus(name); });
}

// The cluster in the address becomes the selected one; false while that is being loaded.
function open(name) {
  if (state.config === name || !state.configs.some((c) => c.name === name)) return true;
  refreshConfigs(name).then(() => { const r = here(); if (r && r.name === name) { render(); readStatus(name); } });
  return false;
}

// ----------------------------------------------------------------------- page

function render() {
  const r = here();
  if (!r) return;
  const c = state.configs.find((x) => x.name === r.name);
  if (!c) {
    P().dataset.view = "";
    return fill(P(), h("div", { class: "empty" }, h("p", {}, `No cluster named ${r.name}`),
      h("button", { class: "secondary", onclick: () => go(`/clusters/${r.cloud}`) }, `${CLOUD[r.cloud] || "All"} clusters`)));
  }
  // The frame (header, banner, tabs, body) is built once per cluster; re-renders refill its parts, and the
  // Configuration form is only built when its tab opens, so polling never resets what is being typed.
  let frame = P().querySelector(":scope > .page");
  if (P().dataset.view !== c.name || !frame) {
    frame = h("div", { class: "page" }, h("div", { class: "x-head" }), h("div", { class: "x-banner" }), h("div", { class: "x-tabs" }), h("div", { class: "x-body" }));
    fill(P(), frame);
    P().dataset.view = c.name;
  }
  const part = (k) => frame.querySelector(`:scope > .x-${k}`);
  fill(part("head"), header(c));
  fill(part("banner"), banner(c.name));
  fill(part("tabs"), tabs(c.name, r.tab));
  const body = part("body");
  if (r.tab === "config") {
    if (body.dataset.tab !== "config") { body.dataset.tab = "config"; renderConfig(body, c.name); }
    return;
  }
  body.dataset.tab = r.tab;
  fill(body, r.tab === "runs" ? runsTab(c.name) : summary(c));
}

// One status for the header pill: [class, text].
// While a run is open its result is not known yet: the pill says so instead of showing the last proof.
function verdict(s, a, busy) {
  if (busy && a) return a.real?.status === "prepared" ? ["warn", "Awaiting approval"] : ["info", `${TITLES[a.which]} running…`];
  if (!s || s.loading) return ["muted", s ? "Reading…" : "Not read"];
  if (s.error) return ["fail", "Unreachable"];
  if ((s.preflight?.checks || []).some((x) => x.status === "fail")) return ["fail", "Needs attention"];
  if (!s.facts?.release) return ["muted", "Not installed"];
  if (s.proof?.proven) return ["ok", "OIDC working"];
  return s.proof?.complete ? ["fail", "OIDC not proven"] : ["warn", "Installed · not verified"];
}

function header(c) {
  const s = live[c.name] || {};
  const f = s.facts || {};
  const cl = f.cluster || {};
  const rel = f.release;
  const busy = isBusy(c.name);
  const [vcls, vtext] = verdict(s, acts[c.name], busy);
  const gke = (c.platform || "aks") === "gke";
  const facts = [c.cluster && c.cluster !== c.name ? c.cluster : "", CLOUD[c.platform || "aks"], cl.location || c.location,
    gke ? c.subscription : [c.subscription, c.resource_group].filter(Boolean).join(" / "),
    cl.kubernetes_version ? `Kubernetes ${cl.kubernetes_version}` : "", rel ? `provider ${rel.chart.replace(/^.*-(?=\d)/, "v")}` : ""];
  return h("header", { class: "page-head" },
    h("div", { class: "page-id" },
      h("div", { class: "page-title" }, h("span", { class: "head-mark" }, markOf("k8s")), h("h1", {}, c.name), h("span", { class: `badge ${vcls}` }, vtext)),
      h("p", { class: "facts" }, facts.filter(Boolean).join(" · "))),
    h("div", { class: "head-actions" },
      // Until the first read says otherwise, the cluster is taken as installed: the button waits on the read.
      h("button", { class: "primary", disabled: busy || s.loading || !s.facts, onclick: () => start(rel ? "verify" : "setup") }, rel || !s.facts ? "Verify" : "Set up"),
      rel ? h("button", { class: "secondary", disabled: busy, onclick: () => start("setup") }, "Set up again") : null,
      moreMenu([
        { label: s.loading ? "Reading…" : "Refresh", disabled: s.loading || busy, onclick: () => readStatus(c.name) },
        { label: "Edit", onclick: () => go(clusterPath(c.name, "config")) },
        { label: "Clear old runs", danger: true, disabled: busy, onclick: () => clearRuns(c.name) },
        rel ? { label: "Tear down", danger: true, disabled: busy, onclick: () => { go(clusterPath(c.name)); start("teardown"); } } : null,
        { label: "Remove from console", danger: true, disabled: busy, onclick: () => remove(c.name) },
      ], "More actions")));
}

// A run that waits for approval shows on every tab, with the way to it.
function banner(name) {
  const r = acts[name]?.real;
  if (!r || r.status !== "prepared") return null;
  return h("div", { class: "approval-banner", role: "status" }, icon("waiting"),
    h("span", {}, h("b", {}, "1 approval needs review"), muted(` · ${runTitle(r)}, prepared ${when(r.created)}`)),
    h("button", { class: "primary", onclick: () => go(runPath(name, r.id)) }, "Review"));
}

function tabs(name, tab) {
  return h("nav", { class: "tabs", "aria-label": "Cluster" }, Object.entries(TABS).map(([k, label]) => h("a", {
    class: "tab", href: url(clusterPath(name, k)), "aria-current": k === tab ? "page" : null,
    onclick: (e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); if (k !== tab) go(clusterPath(name, k)); },
  }, label, k === "runs" && runsOf[name]?.length ? h("span", { class: "count" }, String(runsOf[name].length)) : null)));
}

const card = (title, aside, ...body) => h("section", { class: "card" },
  h("div", { class: "card-head" }, h("h2", {}, title, help(CARD_HELP[title])), aside), ...body);

// Problems first, then the latest action, the OIDC proof and the essentials: always open.
function summary(c) {
  const s = live[c.name] || {};
  const busy = isBusy(c.name);
  const problems = (s.preflight?.checks || []).filter((x) => x.status === "fail" || x.status === "warn");
  return h("div", { class: "stack" },
    s.error ? h("div", { class: "callout fail" }, brief(s.error)) : null,
    s.loading && !s.facts ? muted("Reading the cluster…") : null,
    problems.length ? card("Problems", h("span", { class: "badge fail" }, String(problems.length)), checklist(problems)) : null,
    actionCard(c.name),
    // The proof's run already shows above as the latest action: its steps are the proof, not shown twice.
    busy || (s.proof && acts[c.name]?.real?.created === s.proof.at) ? null : proofCard(c.name, s.proof),
    essentials(c, s.facts || {}));
}

function essentials(c, f) {
  if (!f.cluster && !f.release) return null;
  const cl = f.cluster || {};
  const rel = f.release;
  const sa = f.service_account;
  const gke = (c.platform || "aks") === "gke";
  const pill = (ok, yes, no) => (ok === undefined || ok === null ? muted("—") : h("span", { class: `pill-s ${ok ? "ok" : "fail"}` }, ok ? yes : no));
  const map = Object.values(f.mappings || {}).flat()[0];
  const ttl = map?.token_spec?.expires_in;
  const rows = [
    [gke ? "Project" : "Subscription", c.subscription || muted("—")],
    gke ? null : ["Resource group", c.resource_group || muted("—")],
    ["Location", cl.location || c.location || muted("—")],
    ["Platform", CLOUD[c.platform || "aks"]],
    ["Kubernetes version", cl.kubernetes_version || muted("—")],
    ["Nodes", f.nodes ? String(f.nodes.length) : muted("—")],
    gke ? null : ["OIDC issuer", pill(f.cluster ? cl.oidc_enabled : undefined, "Enabled", "Disabled")],
    ["Workload Identity", pill(f.cluster ? cl.workload_identity : undefined, "Enabled", "Disabled")],
    ["Credential provider", rel ? rel.chart.replace(/^.*-(?=\d)/, "v") : pill(f.cluster ? false : undefined, "", "Not installed")],
    ["JFrog provider", pill(f.providers ? f.providers.some((p) => p.name === c.provider) : undefined, "Present", "Missing")],
    ["Service account", pill(sa ? sa.exists && sa.annotation === "true" : undefined, "Allowed", sa?.exists ? "Not allowed" : "Missing")],
    ["Mapped user", map ? `${map.token_spec?.username || "—"}${ttl ? ` · ${Math.round(ttl / 60)} min tokens` : ""}` : muted("—")],
  ].filter(Boolean);
  return card("Essentials", null, h("dl", { class: "essentials" }, rows.map(([k, v]) => h("div", {}, h("dt", {}, k, help(ESS_HELP[k])), h("dd", {}, v)))));
}

// The OIDC proof: each link of the newest real Set up or Verify that checked it, cluster to JFrog.
const PROOF = ["cluster", "sa-check", "trust", "map", "mapping", "provider", "anon", "pull", "who"];
function latestProof(runs) {
  const run = runs.find((x) => x.kind === "direct" && x.name !== "teardown" && !isDry(x) && ((x.result || {}).steps || []).some((st) => st.id === "pull"));
  if (!run) return null;
  const steps = run.result.steps.filter((st) => PROOF.includes(st.id)).sort((a, b) => PROOF.indexOf(a.id) - PROOF.indexOf(b.id));
  const proven = run.status === "succeeded" && steps.some((st) => st.id === "who" && st.status === "ok")
    && !steps.some((st) => st.status === "failed" || st.status === "warn");
  return { id: run.id, at: run.created, steps, proven, complete: steps.some((st) => st.id === "who") };
}

function proofCard(name, p) {
  if (!p) return null;
  const tag = p.proven ? ["ok", "Working"] : !p.complete ? ["warn", "Incomplete"] : ["fail", "Not proven"];
  return card("OIDC proof", [h("span", { class: `badge ${tag[0]}` }, tag[1]), muted(when(p.at)), runLink(name, p.id)], checklist(p.steps));
}

const runLink = (name, id) => h("span", { class: "card-link" }, link(go, url, runPath(name, id), "View run"));

// ----------------------------------------------------------------------- runs tab

function runsTab(name) {
  const runs = runsOf[name];
  const toolbar = h("div", { class: "tab-bar" },
    h("button", { class: "secondary", onclick: () => refreshAction(name) }, "Refresh"),
    confirmed("Clear old runs", () => clearRuns(name), { cls: "secondary" }));
  if (!runs) return [toolbar, muted("Loading…")];
  if (!runs.length) return [toolbar, h("div", { class: "empty slim" }, h("p", {}, "No runs yet"))];
  return [toolbar, h("table", { class: "table runs-table" },
    h("thead", {}, h("tr", {}, ["", "Run", "Action", "Started", "Duration", "Steps"].map((t, i) => h("th", { scope: "col", class: i ? null : "c-ico", "aria-label": i ? null : "Status" }, t)))),
    h("tbody", {}, runs.map((r) => {
      const openRun = () => go(runPath(name, r.id));
      const started = runStarted(r);
      return h("tr", { tabindex: "0", onclick: openRun, onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) openRun(); } },
        h("td", { class: "c-ico" }, icon(r.status)),
        h("td", { class: "mono" }, link(go, url, runPath(name, r.id), r.id)),
        h("td", {}, runTitle(r)),
        h("td", {}, started ? when(started) : muted(r.status === "prepared" ? "waiting for approval" : r.status)),
        h("td", {}, started && (r.finished || ACTIVE.includes(r.status)) ? duration(started, r.finished) : muted("—")),
        h("td", {}, pips(stepStatuses(r))));
    })))];
}

// ----------------------------------------------------------------------- reads

// One read per cluster at a time: a second call while one is in flight joins it.
const reading = {};
function readStatus(name) {
  return (reading[name] ||= doRead(name).finally(() => { delete reading[name]; }));
}

async function doRead(name) {
  live[name] = { ...(live[name] || {}), loading: true, error: "" };
  render();
  try {
    const [r, { runs }] = await Promise.all([api("/api/preflight", { method: "POST", body: { config: name } }), api("/api/runs")]);
    keep(name, runs);
    live[name] = { facts: r.facts, preflight: r.preflight, proof: latestProof(runsOf[name]) };
  } catch (err) {
    live[name] = { error: err.message };
  }
  render();
  await advance(name);
  schedule(name);
}

function keep(name, runs) {
  runsOf[name] = runs.filter((r) => r.config === name);
  acts[name] = actionOf(runsOf[name]);
}

// ----------------------------------------------------------------------- the automated action

const fresh = (r) => Date.now() - Date.parse(r.created) < 30 * 60 * 1000;

// The newest Set up / Verify / Tear down of a cluster: its real run and the dry run before it.
function actionOf(mine) {
  const acts = mine.filter((r) => r.kind === "direct" && TITLES[r.name]);
  const top = acts[0];
  if (!top) return null;
  if (isDry(top)) return { which: top.name, dry: top, real: null };
  return { which: top.name, dry: dryBefore(acts, top), real: top };
}
export const dryBefore = (runs, real) => runs.find((r) => r.kind === "direct" && r.name === real.name && isDry(r) && r.created <= real.created) || null;

function isBusy(name) {
  const a = acts[name];
  if (driving[name]) return true;
  if (!a) return false;
  if (a.dry && !a.real && (a.dry.status === "prepared" || ACTIVE.includes(a.dry.status) || (a.dry.status === "succeeded" && fresh(a.dry)))) return true;
  return !!(a.real && (a.real.status === "prepared" || ACTIVE.includes(a.real.status)));
}

// Clear old runs: the newest finished run of each action stays, so the page reads the same.
async function clearRuns(name) {
  try { await api(`/api/runs?config=${encodeURIComponent(name)}`, { method: "DELETE" }); } catch { /* the list stays as it was */ }
  await refreshAction(name);
}

// Removes the configuration from the console only: nothing on the cluster or in JFrog changes.
async function remove(name) {
  const cloud = route().cloud;
  await api(`/api/configs/${encodeURIComponent(name)}`, { method: "DELETE" });
  delete live[name];
  await refreshConfigs(null);
  go(`/clusters/${cloud}`);
}

async function refreshAction(name) {
  let runs;
  try { ({ runs } = await api("/api/runs")); } catch { return schedule(name); }
  const was = acts[name]?.real;
  keep(name, runs);
  live[name] = { ...(live[name] || {}), proof: latestProof(runsOf[name]) };
  render();
  await advance(name);
  const now = acts[name]?.real;
  // The real run just ended: read the cluster again for the fresh state.
  if (was && now && was.id === now.id && ACTIVE.includes(was.status) && !ACTIVE.includes(now.status)) readStatus(name);
  schedule(name);
}

// Moves an action on by itself: a dry run starts as soon as it exists (it is read-only), and a passed
// dry run gets its real run prepared for the approval. A refresh in between resumes here.
async function advance(name) {
  const a = acts[name];
  if (!a || driving[name]) return;
  driving[name] = true;
  try {
    if (a.dry && !a.real && a.dry.status === "prepared") {
      a.dry = await api(`/api/runs/${a.dry.id}/approve`, { method: "POST", body: { request_sha256: a.dry.request_sha256, approved: true } });
    }
    if (a.dry && !a.real && a.dry.status === "succeeded" && fresh(a.dry)) {
      a.real = await api("/api/runs", { method: "POST", body: { config: name, kind: "direct", name: a.which, params: { DryRun: false } } });
    }
  } catch (err) {
    a.error = err.message;
  } finally {
    driving[name] = false;
  }
  render();
}

function schedule(name) {
  clearTimeout(timer);
  const a = acts[name];
  const moving = a && [a.dry, a.real].some((r) => r && ACTIVE.includes(r.status));
  if (moving) timer = setTimeout(() => { if (here()?.name === name) refreshAction(name); }, 2000);
}

async function start(which) {
  const name = state.config;
  driving[name] = true;
  render();
  try {
    const dry = await api("/api/runs", { method: "POST", body: { config: name, kind: "direct", name: which, params: { DryRun: true } } });
    acts[name] = { which, dry, real: null };
    if (!dry.guards.length) {
      acts[name].dry = await api(`/api/runs/${dry.id}/approve`, { method: "POST", body: { request_sha256: dry.request_sha256, approved: true } });
    }
  } catch (err) {
    acts[name] = { which, dry: null, real: null, error: err.message };
  } finally {
    driving[name] = false;
  }
  refreshAction(name);
}

function actionCard(name) {
  const a = acts[name];
  if (!a) return null;
  const { dry: d, real: r } = a;
  const stages = [], parts = [];
  if (d) {
    if (d.status === "refused") stages.push([brief(d.guards.join(" ")), "error"]);
    else if (d.status === "prepared" || ACTIVE.includes(d.status)) { stages.push(["Dry run…", ""]); parts.push(progress(d)); }
    else if (d.status === "succeeded") stages.push(["Dry run passed", "ok"]);
    else {
      stages.push([d.status === "interrupted" ? "Dry run interrupted (the console restarted)" : "Dry run failed", "error"]);
      parts.push(stepTable(d.result), causeEl(name, d));
    }
    if (d.status === "succeeded" && !r) stages.push(fresh(d) ? ["Preparing the approval…", ""] : ["Dry run expired: start again", "muted"]);
  }
  if (r) {
    if (r.status === "refused") stages.push([brief(r.guards.join(" ")), "error"]);
    else if (r.status === "prepared") { stages.push(["Waiting for approval", ""]); parts.push(approvalBox(name, r, d, () => refreshAction(name))); }
    else if (r.status === "cancelled") stages.push(["Cancelled: nothing changed", "muted"]);
    else {
      stages.push([`Approved ${when(r.approved?.at)}`, "ok"]);
      if (ACTIVE.includes(r.status)) { stages.push(["Applying…", ""]); parts.push(progress(r)); }
      else if (r.status === "succeeded") { stages.push([a.which === "teardown" ? "Removed" : "Applied", "ok"]); parts.push(confirmation(a.which, r), stepTable(r.result)); }
      else {
        stages.push([r.status === "interrupted" ? "Interrupted (the console restarted)" : "Failed", "error"]);
        parts.push(r.error ? h("p", { class: "error small" }, brief(r.error)) : null, stepTable(r.result), causeEl(name, r));
      }
    }
  }
  if (a.error) parts.push(h("p", { class: "error small" }, brief(a.error)));
  const shown = r || d;
  return h("section", { class: `card${r?.status === "prepared" ? " needs-you" : ""}` },
    h("div", { class: "card-head" }, h("h2", {}, TITLES[a.which], help(CARD_HELP[TITLES[a.which]])),
      h("ol", { class: "stages" }, stages.map(([t, cls]) => h("li", { class: cls }, t))),
      muted(when(shown?.created)), shown?.id ? runLink(name, shown.id) : null),
    ...parts);
}

// The steps as they run: one row per plan step, updated in place from the run's own record.
function progress(run) {
  const by = liveSteps(run);
  const done = Object.fromEntries((((run.result || {}).steps) || []).map((s) => [s.id, s]));
  const detail = {};
  for (const e of run.events || []) {
    const m = /^[✓✗•] (.*)$/.exec(e.text || "");
    const st = m && (run.request.parameters.steps || []).find((s) => m[1].startsWith(`${s.title}: `));
    if (st) detail[st.id] = m[1].slice(st.title.length + 2);
  }
  return checklist((run.request.parameters.steps || []).map((s) => ({ title: s.title, status: done[s.id]?.status || by[s.id] || "", detail: done[s.id]?.detail ?? detail[s.id] ?? "" })));
}

// The confirmation: what the evidence says, in one line.
function confirmation(which, run) {
  const steps = (run.result || {}).steps || [];
  const by = Object.fromEntries(steps.map((s) => [s.id, s]));
  const bad = steps.find((s) => s.status === "failed" || s.status === "warn");
  if (which === "teardown") {
    return by.gone?.status === "ok"
      ? h("div", { class: "callout ok" }, h("strong", {}, "Removed. "), "Checked: nothing left on the cluster or JFrog.")
      : h("div", { class: "callout fail" }, h("strong", {}, "Not confirmed. "), brief(bad ? `${bad.title}: ${bad.detail}` : "the final check did not run"));
  }
  return by.who?.status === "ok" && !bad
    ? h("div", { class: "callout ok" }, h("strong", {}, "OIDC working. "), "Pulled with no pull secret; JFrog recorded the mapped user.")
    : h("div", { class: "callout fail" }, h("strong", {}, "OIDC not proven. "), brief(bad ? `${bad.title}: ${bad.detail}` : "the proof did not finish"));
}

// The plan and the dry-run result for each step, the request hash, one approve button (and the typed
// cluster name for a tear down). Shared with the run page; done() runs after an approve or a cancel.
export function approvalBox(name, run, dry, done) {
  if (boxes[run.id]) { boxes[run.id].done = done; return boxes[run.id].el; }
  const dryById = Object.fromEntries(((dry || {}).result?.steps || []).map((s) => [s.id, s]));
  const typed = h("input", { type: "text", class: "typed", placeholder: run.cluster, autocomplete: "off", "aria-label": "Type the cluster name" });
  const yes = h("button", { class: "primary" }, run.needs_typed_cluster ? "Approve and tear down" : "Approve and apply");
  const no = h("button", { class: "secondary" }, "Cancel");
  const msg = h("p", { class: "error small" });
  const el = h("div", { class: "approve-box" },
    checklist(run.request.parameters.steps.map((s) => ({ status: dryById[s.id]?.status || "", title: s.title, detail: dryById[s.id]?.detail }))),
    h("p", { class: "small muted" }, "Request ", h("code", {}, run.request_sha256.slice(0, 12)), dry ? ` · dry run ${dry.status} ${when(dry.created)}` : ""),
    h("div", { class: "approve" }, run.needs_typed_cluster ? [h("label", { class: "small" }, `Type ${run.cluster} to confirm`), typed] : null, yes, no), msg);
  const box = { el, done };
  const act = async (path, body) => {
    yes.disabled = no.disabled = true;
    try {
      await api(`/api/runs/${run.id}/${path}`, { method: "POST", body });
      delete boxes[run.id];
      box.done();
    } catch (err) {
      yes.disabled = no.disabled = false;
      msg.textContent = brief(err.message);
    }
  };
  yes.onclick = () => act("approve", { request_sha256: run.request_sha256, approved: true, typed_cluster: typed.value });
  no.onclick = () => act("cancel", {});
  boxes[run.id] = box;
  return el;
}

function stepTable(result) {
  const steps = (result || {}).steps || [];
  return steps.length ? checklist(steps) : null;
}

// The cause of a failed run, from its evidence; read once per run.
function causeEl(name, run) {
  if (!(run.id in causes)) {
    causes[run.id] = null;
    api("/api/diagnose", { method: "POST", body: { config: name, text: "" } })
      .then((d) => { causes[run.id] = (d.findings || []).find((f) => f.level === "cause")?.title || ""; render(); })
      .catch(() => { causes[run.id] = ""; });
  }
  return causes[run.id] ? h("div", { class: "callout fail" }, h("strong", {}, "Cause: "), brief(causes[run.id])) : null;
}
