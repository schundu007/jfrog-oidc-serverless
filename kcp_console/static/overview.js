// Clusters: pick or add a cluster, see its live state, and run Set up / Verify / Tear down.
// Each action runs itself: dry run, then the plan for one approval, then the real run, then the
// evidence and a fresh status read. The action is rebuilt from the server's run records, so a page
// refresh or another page in between finds it where it was. The approval is the only click inside it.

import { api, h, fill, state, selectTab, refreshConfigs, route, go, clusterPath, CLOUDS as CLOUD } from "./app.js";
import { $, workflow, brief } from "./ui.js";

const live = {};            // config name -> {loading, facts, preflight, error}
const acts = {};            // config name -> the newest action: {which, dry, real, error}
const boxes = {};           // real run id -> its approval box (kept, so a re-render keeps what was typed)
const causes = {};          // failed run id -> the diagnosed cause
const driving = {};         // config name -> true while this page creates or approves a run
let timer = null;

const TITLES = { setup: "Set up", verify: "Verify", teardown: "Tear down" };

export function initOverview() {
  state.on("config", (name) => { render(); if (!detailName()) return; if (name && !live[name]) readStatus(name); else if (name) refreshAction(name); });
  state.on("tab", (tab) => {
    if (tab !== "overview") return;
    readQuery();
    render();
    if (!detailName()) loadFleet();
    else if (state.config) refreshAction(state.config);
  });
  readQuery();
  render();
  if (detailName() && state.config) readStatus(state.config);
}

const muted = (t) => h("span", { class: "muted" }, t);
const when = (iso) => (iso || "").slice(0, 16).replace("T", " ");

// Two views: one platform's clusters at /clusters/<cloud> and one cluster at /clusters/<cloud>/<name>.
const detailName = () => route().name;
const cloud = () => route().cloud || "aks";

function render() {
  const P = $("panel-overview");
  const name = detailName();
  if (!name) return fill(P, fleetView());
  const c = (state.configs || []).find((x) => x.name === name);
  if (c && state.config !== name) { select(name); return; }
  fill(P, c ? clusterView(c)
    : h("div", { class: "empty" }, h("p", {}, `No cluster named ${name}`), h("button", { class: "secondary", onclick: openFleet }, `${CLOUD[cloud()]} clusters`)));
}

export function openCluster(name) {
  go(clusterPath(name));
}

function openFleet() {
  go(`/clusters/${cloud()}${query()}`);
}

// ----------------------------------------------------------------------- the fleet

const PAGE = 25;
const STATE = {
  approval: ["warn", "Awaiting approval"], running: ["info", "Running…"], attention: ["fail", "Needs attention"], not_proven: ["fail", "OIDC not proven"],
  unverified: ["warn", "Not verified"], working: ["ok", "OIDC working"], not_installed: ["muted", "Not set up"], unread: ["muted", "Not read"],
};
const STATS = [["", "All clusters"], ["working", "OIDC working"], ["attention", "Needs attention"], ["not_proven", "Not proven"],
  ["approval", "Awaiting approval"], ["not_installed", "Not set up"]];
const ACTION = { prepared: "waiting for approval", queued: "starting", running: "running", succeeded: "done", failed: "failed",
  cancelled: "cancelled", interrupted: "interrupted", refused: "refused", error: "failed" };
const F = { q: "", state: "", page: 1 };
let fleet = null;
let fleetLoading = false;
let adding = false;

function readQuery() {
  const u = new URLSearchParams(location.search);
  Object.assign(F, { q: u.get("q") || "", state: u.get("state") || "", page: Number(u.get("page")) || 1 });
}

function query() {
  const u = new URLSearchParams();
  for (const k of ["q", "state"]) if (F[k]) u.set(k, F[k]);
  if (F.page > 1) u.set("page", F.page);
  return u.toString() ? `?${u}` : "";
}

// Filters live in the address, so a refresh or a shared link shows the same list.
function setFilter(patch) {
  Object.assign(F, { page: 1 }, patch);
  history.replaceState({}, "", `/clusters/${cloud()}${query()}`);
}

async function loadFleet() {
  if (fleetLoading) return;
  fleetLoading = true;
  try { fleet = await api("/api/fleet"); } catch (err) { fleet = { clusters: [], counts: {}, error: err.message }; }
  fleetLoading = false;
  if (!detailName() && !$("panel-overview").hidden) render();
}

function ago(iso) {
  if (!iso) return "";
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}

function matches(r) {
  if (F.state && r.state !== F.state) return false;
  const q = F.q.trim().toLowerCase();
  return !q || [r.name, r.cluster, r.location, r.subscription, r.resource_group].some((v) => (v || "").toLowerCase().includes(q));
}

function fleetView() {
  if (!fleet && !fleetLoading) loadFleet();
  const d = fleet || { clusters: [], platforms: {} };
  const k = cloud(), label = CLOUD[k];
  const built = (d.platforms || {})[k] !== false && !!SOURCES[k];
  const mine = d.clusters.filter((r) => (r.cloud || "aks") === k);
  const counts = {};
  for (const r of mine) counts[r.state] = (counts[r.state] || 0) + 1;
  const n = mine.length;
  const open = built && fleet && (adding || !n);   // no clusters yet: the list to add from is the page
  const where = k === "gke" ? "project or location" : "subscription, resource group or region";
  return h("div", { class: "fleet" },
    h("header", { class: "cl-head" },
      h("div", { class: "cl-title" }, h("h1", {}, label), n ? muted(`${n} cluster${n === 1 ? "" : "s"}`) : null),
      h("div", { class: "cl-actions" },
        built ? h("button", { class: "ghost", disabled: fleetLoading, onclick: () => { fleet = null; render(); } }, "Refresh") : null,
        built && n ? h("button", { class: adding ? "secondary" : "primary", onclick: () => { adding = !adding; render(); } }, adding ? "Close" : "Add a cluster") : null)),
    d.error ? h("div", { class: "callout fail" }, brief(d.error)) : null,
    !built ? h("div", { class: "empty" }, h("p", {}, `${label} is not supported yet`)) : null,
    open ? addPanel(state.configs || [], k) : null,
    !built ? null
      : !fleet ? h("p", { class: "muted" }, "Loading…")
      : n ? [
        h("div", { class: "stats" }, STATS.map(([s, text]) => h("button", {
          class: `stat${F.state === s ? " on" : ""}${s ? ` s-${s}` : ""}`, "aria-pressed": F.state === s ? "true" : "false",
          onclick: () => { setFilter({ state: F.state === s ? "" : s }); render(); },
        }, h("b", {}, String(s ? counts[s] || 0 : n)), h("span", {}, text)))),
        h("div", { class: "fleet-bar" },
          h("input", { type: "search", class: "search", placeholder: `Search by name, ${where}`, value: F.q, "aria-label": "Search clusters",
            oninput: (e) => { setFilter({ q: e.target.value }); fillTable(); } })),
        h("div", { id: "fleet-table" }, fleetTable(mine)),
      ] : null);
}

function fillTable() {
  const el = $("fleet-table");
  if (el && fleet) fill(el, fleetTable(fleet.clusters.filter((r) => (r.cloud || "aks") === cloud())));
}

function fleetTable(all) {
  const rows = all.filter(matches);
  const gke = cloud() === "gke";
  if (!rows.length) return h("div", { class: "empty slim" }, h("p", {}, "No clusters match"),
    h("button", { class: "ghost", onclick: () => { setFilter({ q: "", state: "" }); render(); } }, "Clear filters"));
  const pages = Math.ceil(rows.length / PAGE);
  F.page = Math.min(Math.max(1, F.page), pages);
  const first = (F.page - 1) * PAGE;
  const go = (p) => { F.page = p; history.replaceState({}, "", `/clusters/${cloud()}${query()}`); fillTable(); };
  return [
    h("table", { class: "fleet-table" },
      h("thead", {}, h("tr", {}, (gke ? ["Cluster", "Project", "Status", "Credential provider", "Last verified", "Last action"]
        : ["Cluster", "Subscription", "Resource group", "Status", "Credential provider", "Last verified", "Last action"]).map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.slice(first, first + PAGE).map((r) => {
        const [cls, text] = STATE[r.state] || ["muted", r.state];
        const a = r.action;
        return h("tr", { tabindex: "0", onclick: () => openCluster(r.name), onkeydown: (e) => { if (e.key === "Enter") openCluster(r.name); } },
          h("td", {}, h("div", { class: "nm" }, r.name), h("div", { class: "sub" }, [r.cluster !== r.name ? r.cluster : "", r.location].filter(Boolean).join(" · "))),
          h("td", { class: "wrap" }, r.subscription || muted("—")),
          gke ? null : h("td", { class: "wrap" }, r.resource_group || muted("—")),
          h("td", {}, h("span", { class: `badge ${cls}` }, text), r.problem ? h("div", { class: "sub" }, brief(r.problem)) : null),
          h("td", {}, r.chart ? h("div", {}, `v${r.chart}`) : muted("—"), r.ready ? h("div", { class: "sub" }, `${r.ready} nodes ready`) : null),
          h("td", {}, r.verified_at ? ago(r.verified_at) : muted("never")),
          h("td", {}, a ? [h("div", {}, `${TITLES[a.which] || a.which}${a.dry ? " · dry run" : ""}`), h("div", { class: "sub" }, `${ACTION[a.status] || a.status} · ${ago(a.at)}`)] : muted("—")));
      }))),
    pages > 1 ? h("div", { class: "pager" },
      muted(`${first + 1}–${Math.min(first + PAGE, rows.length)} of ${rows.length}`),
      h("button", { class: "ghost", disabled: F.page <= 1, onclick: () => go(F.page - 1) }, "Previous"),
      h("button", { class: "ghost", disabled: F.page >= pages, onclick: () => go(F.page + 1) }, "Next")) : null,
  ];
}

// One status for the header badge: [class, text].
// While a run is open its result is not known yet: the badge says so instead of showing the last proof.
function verdict(s, a, busy) {
  if (busy && a) return a.real?.status === "prepared" ? ["warn", "Awaiting approval"] : ["info", `${TITLES[a.which]} running…`];
  if (!s || s.loading) return ["muted", s ? "Reading…" : "Not read"];
  if (s.error) return ["fail", "Unreachable"];
  if ((s.preflight?.checks || []).some((c) => c.status === "fail")) return ["fail", "Needs attention"];
  if (!s.facts?.release) return ["muted", "Not installed"];
  if (s.proof?.proven) return ["ok", "OIDC working"];
  return s.proof?.complete ? ["fail", "OIDC not proven"] : ["warn", "Installed · not verified"];
}

function clusterView(c) {
  const s = live[c.name] || {};
  const f = s.facts || {};
  const rel = f.release;
  const busy = isBusy(c.name);
  const [vcls, vtext] = verdict(s, acts[c.name], busy);
  const problems = (s.preflight?.checks || []).filter((x) => x.status === "fail" || x.status === "warn");
  const sa = f.service_account;
  // Essentials, as on a cloud portal's resource page: label/value pairs in two columns, state as pills.
  const cl = f.cluster || {};
  const gke = (c.platform || "aks") === "gke";
  const pill = (ok, yes, no) => (ok === undefined || ok === null ? muted("—") : h("span", { class: `pill-s ${ok ? "ok" : "fail"}` }, ok ? yes : no));
  const map = Object.values(f.mappings || {}).flat()[0];
  const ttl = map?.token_spec?.expires_in;
  const essentials = [
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
  const facts = f.cluster || rel ? h("section", { class: "panel essentials" },
    h("div", { class: "panel-head" }, h("h2", {}, "Essentials")),
    h("dl", { class: "ess" }, essentials.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v))))) : null;
  return h("div", { class: "cluster" },
    h("nav", { class: "crumbs", "aria-label": "Breadcrumb" },
      h("span", {}, "Clusters"), h("span", {}, "/"),
      h("a", { href: `/clusters/${cloud()}`, onclick: (e) => { e.preventDefault(); openFleet(); } }, CLOUD[cloud()]), h("span", {}, "/"), h("span", {}, c.name)),
    h("header", { class: "cl-head" },
      h("div", {},
        h("div", { class: "cl-title" },
          h("h1", {}, c.name),
          h("span", { class: `badge ${vcls}` }, vtext),
          c.cluster && c.cluster !== c.name ? muted(c.cluster) : null)),
      h("div", { class: "cl-actions" },
        h("button", { class: "ghost", disabled: s.loading || busy, onclick: () => readStatus(c.name) }, s.loading ? "Reading…" : "Refresh"),
        h("button", { class: "ghost", disabled: busy, onclick: () => selectTab("configure") }, "Edit"),
        rel ? h("button", { class: "ghost danger", disabled: busy, onclick: () => start("teardown") }, "Tear down") : null,
        rel ? h("button", { class: "secondary", disabled: busy, onclick: () => start("setup") }, "Set up again") : null,
        h("button", { class: "primary", disabled: busy, onclick: () => start(rel ? "verify" : "setup") }, rel ? "Verify" : "Set up"))),
    s.error ? h("div", { class: "callout fail" }, brief(s.error)) : null,
    facts,
    problems.length ? panel("Problems", null, checklist(problems)) : null,
    actionEl(c.name),
    // The proof's run already shows above as the latest action: its steps are the proof, not shown twice.
    busy || (s.proof && acts[c.name]?.real?.created === s.proof.at) ? null : proofBlock(s.proof));
}

const panel = (title, aside, ...body) => h("section", { class: "panel" },
  h("div", { class: "panel-head" }, h("h2", {}, title), aside), ...body);

// Steps or checks as a list with a status mark: {status, title, detail}.
const MARK = { running: "", pending: "", ok: "✓", done: "✓", pass: "✓", failed: "✕", fail: "✕", error: "✕", warn: "!", skipped: "–" };
function checklist(items) {
  return h("ul", { class: "checks" }, h("li", { class: "head", "aria-hidden": "true" }, h("span", {}), h("span", {}, "Item"), h("span", {}, "Result")), items.map((x) => {
    const st = String(x.status === "done" ? "ok" : x.status || "").toLowerCase();
    return h("li", { class: `st-${st}` }, h("span", { class: "mark", title: st }, MARK[st] || "•"),
      h("span", { class: "t" }, x.title || x.id), h("span", { class: "d" }, brief(x.detail)));
  }));
}

// The OIDC proof: each link of the newest real Set up or Verify that checked it, cluster to JFrog.
const PROOF = ["cluster", "sa-check", "trust", "map", "mapping", "provider", "anon", "pull", "who"];
function latestProof(runs, name) {
  const run = runs.find((x) => x.config === name && x.kind === "direct" && x.name !== "teardown"
    && x.request.parameters.DryRun === false && ((x.result || {}).steps || []).some((st) => st.id === "pull"));
  if (!run) return null;
  const steps = run.result.steps.filter((st) => PROOF.includes(st.id)).sort((a, b) => PROOF.indexOf(a.id) - PROOF.indexOf(b.id));
  const proven = run.status === "succeeded" && steps.some((st) => st.id === "who" && st.status === "ok")
    && !steps.some((st) => st.status === "failed" || st.status === "warn");
  return { at: run.created, steps, proven, complete: steps.some((st) => st.id === "who") };
}

function proofBlock(p) {
  if (!p) return null;
  const tag = p.proven ? ["ok", "Working"] : !p.complete ? ["warn", "Incomplete"] : ["fail", "Not proven"];
  return h("details", { class: "panel proof", open: !p.proven },
    h("summary", { class: "panel-head" }, h("h2", {}, "OIDC"), h("span", { class: `badge ${tag[0]}` }, tag[1]), muted(when(p.at))),
    checklist(p.steps));
}

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
    live[name] = { facts: r.facts, preflight: r.preflight, proof: latestProof(runs, name) };
    acts[name] = actionOf(runs, name);
  } catch (err) {
    live[name] = { error: err.message };
  }
  render();
  await advance(name);
  schedule(name);
}

// ----------------------------------------------------------------------- the automated action

const ACTIVE = ["queued", "running"];
const dryOf = (r) => r.request.parameters.DryRun !== false;
const fresh = (r) => Date.now() - Date.parse(r.created) < 30 * 60 * 1000;

// The newest Set up / Verify / Tear down of a cluster: its real run and the dry run before it.
function actionOf(runs, name) {
  const mine = runs.filter((r) => r.config === name && r.kind === "direct" && TITLES[r.name]);
  const top = mine[0];
  if (!top) return null;
  if (dryOf(top)) return { which: top.name, dry: top, real: null };
  return { which: top.name, dry: mine.find((r) => r.name === top.name && dryOf(r) && r.created <= top.created) || null, real: top };
}

function isBusy(name) {
  const a = acts[name];
  if (driving[name]) return true;
  if (!a) return false;
  if (a.dry && !a.real && (a.dry.status === "prepared" || ACTIVE.includes(a.dry.status) || (a.dry.status === "succeeded" && fresh(a.dry)))) return true;
  return !!(a.real && (a.real.status === "prepared" || ACTIVE.includes(a.real.status)));
}

async function refreshAction(name) {
  let runs;
  try { ({ runs } = await api("/api/runs")); } catch { return schedule(name); }
  const was = acts[name]?.real;
  acts[name] = actionOf(runs, name);
  live[name] = { ...(live[name] || {}), proof: latestProof(runs, name) };
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
  if (moving && state.config === name) timer = setTimeout(() => { if (state.config === name) refreshAction(name); }, 2000);
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

function actionEl(name) {
  const a = acts[name];
  if (!a) return null;
  const { dry: d, real: r } = a;
  const stages = [], parts = [];
  if (d) {
    if (d.status === "refused") stages.push([brief(d.guards.join(" ")), "error"]);
    else if (d.status === "prepared" || ACTIVE.includes(d.status)) stages.push(["Dry run…", ""]);
    else if (d.status === "succeeded") stages.push(["Dry run passed", "ok"]);
    else {
      stages.push([d.status === "interrupted" ? "Dry run interrupted (the console restarted)" : "Dry run failed", "error"]);
      parts.push(stepTable(d.result), causeEl(name, d));
    }
    if (d.status === "succeeded" && !r) stages.push(fresh(d) ? ["Preparing the approval…", ""] : ["Dry run expired: start again", "muted"]);
  }
  if (r) {
    if (r.status === "refused") stages.push([brief(r.guards.join(" ")), "error"]);
    else if (r.status === "prepared") { stages.push(["Waiting for approval", ""]); parts.push(approvalBox(name, r, d)); }
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
  return panel(TITLES[a.which], muted(when((r || d || {}).created)),
    h("ol", { class: "stages" }, stages.map(([t, cls]) => h("li", { class: cls }, t))),
    ...parts);
}

// The steps as they run: one row per plan step, updated in place from the run's own record.
function progress(run) {
  const by = {};
  for (const e of run.events || []) {
    if (!workflow(e.text)) continue;
    const m = e.text.match(/^([▶✓✗•■]) (.*)$/);
    if (!m) continue;
    const step = (run.request.parameters.steps || []).find((s) => m[2] === s.title || m[2].startsWith(`${s.title}: `));
    if (!step) continue;
    const detail = m[2].slice(step.title.length + 2);
    by[step.id] = m[1] === "▶" ? { status: "running", detail: "" }
      : { status: m[1] === "✓" ? "ok" : m[1] === "✗" ? "failed" : "skipped", detail };
  }
  return checklist((run.request.parameters.steps || []).map((s) => ({ title: s.title, ...(by[s.id] || { status: "pending", detail: "" }) })));
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

// The plan and the dry-run result for each step, one approve button (and the typed cluster name for a tear down).
function approvalBox(name, run, dry) {
  if (boxes[run.id]) return boxes[run.id];
  const dryById = Object.fromEntries(((dry || {}).result?.steps || []).map((s) => [s.id, s]));
  const typed = h("input", { type: "text", class: "typed", placeholder: run.cluster, autocomplete: "off", "aria-label": "Type the cluster name" });
  const go = h("button", { class: "primary" }, run.needs_typed_cluster ? "Approve and tear down" : "Approve and apply");
  const no = h("button", { class: "secondary" }, "Cancel");
  const msg = h("p", { class: "error small" });
  const box = h("div", { class: "approve-box" },
    checklist(run.request.parameters.steps.map((s) => ({ status: dryById[s.id]?.status || "", title: s.title, detail: dryById[s.id]?.detail }))),
    h("div", { class: "approve" }, run.needs_typed_cluster ? typed : null, go, no), msg);
  const act = async (path, body) => {
    go.disabled = no.disabled = true;
    try {
      await api(`/api/runs/${run.id}/${path}`, { method: "POST", body });
      delete boxes[run.id];
      refreshAction(name);
    } catch (err) {
      go.disabled = no.disabled = false;
      msg.textContent = brief(err.message);
    }
  };
  go.onclick = () => act("approve", { request_sha256: run.request_sha256, approved: true, typed_cluster: typed.value });
  no.onclick = () => act("cancel", {});
  return (boxes[run.id] = box);
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

// ----------------------------------------------------------------------- add a cluster

// Existing clusters the operator's logins can see, per cloud: every subscription (az) or project (gcloud).
const SOURCES = { aks: { api: "/api/aks", short: "AKS" }, gke: { api: "/api/gke", short: "GKE" } };
const found = {};           // cloud -> {clusters, loading, error}

function addPanel(configs, cloud) {
  return panel(`Add a ${SOURCES[cloud].short} cluster`, null, foundTable(configs, cloud));
}

function foundTable(configs, cloud) {
  const src = SOURCES[cloud], f = found[cloud];
  const added = new Set(configs.map((c) => (c.cluster || "").toLowerCase()));
  if (!f) { loadFound(cloud); return muted(`Finding ${src.short} clusters…`); }
  if (f.loading) return muted(`Finding ${src.short} clusters…`);
  if (f.error) return h("p", { class: "error small" }, brief(f.error));
  const rows = f.clusters.filter((k) => !added.has(k.name.toLowerCase()));
  if (!rows.length) return muted(f.clusters.length ? `All ${src.short} clusters are added.` : `No ${src.short} clusters found.`);
  const on = (b) => (b ? "on" : h("span", { class: "bad" }, "off"));
  const cols = cloud === "gke"
    ? ["Cluster", "Project", "Location", "Kubernetes", "Workload Identity", ""]
    : ["Cluster", "Subscription", "Resource group", "Location", "Kubernetes", "OIDC · WI", ""];
  return h("table", { class: "cfg-table" },
    h("thead", {}, h("tr", {}, cols.map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", {}, rows.map((k) => h("tr", {},
      h("td", {}, h("b", {}, k.name), k.power && k.power !== "Running" ? muted(` ${k.power}`) : null,
        k.status && k.status !== "RUNNING" ? muted(` ${k.status.toLowerCase()}`) : null),
      ...(cloud === "gke"
        ? [h("td", {}, k.project_name && k.project_name !== k.project_id ? [k.project_name, h("div", { class: "sub" }, k.project_id)] : k.project_id),
           h("td", {}, k.location), h("td", {}, k.version), h("td", {}, k.autopilot ? muted("Autopilot") : on(k.workload_identity))]
        : [h("td", {}, k.subscription_name || k.subscription_id), h("td", {}, k.resource_group), h("td", {}, k.location), h("td", {}, k.version),
           h("td", {}, on(k.oidc_issuer), " · ", on(k.workload_identity))]),
      h("td", { class: "row-actions" }, k.autopilot
        ? muted("not supported")
        : h("button", { class: "primary small-btn", onclick: () => add(k, cloud) }, "Add"))))));
}

async function loadFound(cloud) {
  found[cloud] = { clusters: [], loading: true };
  try { found[cloud] = await api(SOURCES[cloud].api); } catch (err) { found[cloud] = { clusters: [], error: err.message }; }
  render();
}

// A new configuration for an existing cluster: its coordinates from the cloud, the JFrog side copied from
// the newest saved cluster (same JFrog instance, user and test image), the provider and mapping named after
// it. Set up then adopts what the cluster and JFrog already have and creates only what is missing.
async function add(k, cloud) {
  const cfg = await api("/api/defaults");
  const name = k.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 63);
  cfg.name = name;
  cfg.platform = cloud;
  if (cloud === "gke") {
    Object.assign(cfg.cluster, { name: k.name, project_id: k.project_id, location: k.location, subscription_name: k.project_name || k.project_id,
      resource_group: "", subscription_id: "", google_service_account: "", oidc_issuer: "" });   // the server derives GKE's issuer
  } else {
    Object.assign(cfg.cluster, { name: k.name, resource_group: k.resource_group, subscription_id: k.subscription_id,
      subscription_name: k.subscription_name || "", oidc_issuer: k.oidc_issuer });
  }
  const newest = [...(state.configs || [])].sort((a, b) => (b.updated || "").localeCompare(a.updated || ""))[0];
  if (newest) {
    const src = await api(`/api/configs/${encodeURIComponent(newest.name)}`);
    const o = src.config || src;
    for (const f of ["host", "mapped_user", "test_repo", "test_repo_host", "test_image", "scope", "audience", "expires_in"]) cfg.jfrog[f] = o.jfrog[f];
    for (const f of ["artifactory_url", "match_images", "chart_version", "binary_version", "default_cache_duration"]) cfg.values[f] = o.values[f];
  }
  cfg.jfrog.oidc_provider = cfg.values.provider_name = `${name}-oidc`.slice(0, 63);
  cfg.jfrog.mapping_name = `${name}-mapping`.slice(0, 63);
  await api(`/api/configs/${encodeURIComponent(name)}`, { method: "PUT", body: { config: cfg } });
  delete found[cloud];
  adding = false;
  fleet = null;
  await refreshConfigs(name);
  if (!cfg.jfrog.host) selectTab("configure"); else openCluster(name);
}

async function select(name) {
  await refreshConfigs(name);
  state.emit("config", name);
}
