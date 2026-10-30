"""The exportable report (spec §7 `GET /api/report/<config>`): Markdown or JSON.

It states what was verified and what was not, using the same data the tabs
show: the config and values file, the latest Preflight, the diagnosis and
three-layer diff, the pull verdict and its proof, audit attribution, the
security rows and open items with their recorded status, and the runbook's
testing performed / not performed lists. It is redacted before it leaves.
"""

from __future__ import annotations

import json

from . import knowledge, model, redact


def build(cfg: dict, status: dict, diagnosis: dict, preflight: dict | None, runs: list) -> dict:
    cfg = model.merged(cfg)
    v = diagnosis["verdict"]
    sec = (status or {}).get("security") or {}
    items = (status or {}).get("open_items") or {}
    return redact.obj({
        "title": f"KCP report: {cfg['cluster']['name']} ({cfg['name']})",
        "runbook": knowledge.RUNBOOK,
        "config": {"name": cfg["name"], "mode": cfg["mode"], "cluster": cfg["cluster"], "k8s": cfg["k8s"], "jfrog": cfg["jfrog"]},
        "values_file": model.render_values(cfg), "values_sha256": model.values_sha256(cfg),
        "preflight": preflight,
        "verdict": {"status": v["status"], "root_cause": v["root_cause"],
                    "proof": v.get("proof"), "control": v.get("control"), "audit": v.get("audit")},
        "findings": [{k: f[k] for k in ("rule", "score", "title", "evidence", "label", "next")} for f in diagnosis["findings"]],
        "three_layers": diagnosis.get("three_layers"),
        "security": [{**r, "recorded": (sec.get(r["id"]) or {}).get("status", "open"), "note": (sec.get(r["id"]) or {}).get("note", "")}
                     for r in knowledge.SECURITY_REVIEW["rows"]],
        "open_items": [{**o, "recorded": (items.get(o["id"]) or {}).get("status", "open"), "note": (items.get(o["id"]) or {}).get("note", "")}
                       for o in knowledge.OPEN_ITEMS],
        "testing_performed": knowledge.TESTING_PERFORMED, "testing_not_performed": knowledge.TESTING_NOT_PERFORMED,
        "runs": [{k: r.get(k) for k in ("id", "created", "kind", "name", "status", "changes_state", "ado_run_id")} for r in runs],
        "where_things_stand": knowledge.WHERE_THINGS_STAND,
    })


def markdown(rep: dict) -> str:
    L = [f"# {rep['title']}", "", f"Runbook: {rep['runbook']['title']} (updated {rep['runbook']['updated']})", "",
         f"> {rep['where_things_stand']}", "", "## Verdict", ""]
    v = rep["verdict"]
    L.append(f"- Latest test pod: **{v['status']}**" + (f" (root cause: `{v['root_cause']}`)" if v["root_cause"] else ""))
    if v.get("proof"):
        p = v["proof"]
        L.append(f"- Proof of a secretless pull: {p.get('evidence')} on {p.get('node')}. {p.get('exchange', '')}")
    if v.get("control"):
        L.append(f"- Control pod ({v['control']['sa']}): {'refused, as intended' if v['control']['refused'] else 'PULLED: access is not tied to the identity'}")
    a = v.get("audit") or {}
    L.append(f"- Audit attribution: **{a.get('status', 'NOT RUN')}**. {a.get('detail', '')}")
    if rep["preflight"]:
        L += ["", "## Preflight", "", "| Check | Status | Detail |", "|---|---|---|"]
        L += [f"| {c['title']} | {c['status']} | {c['detail']} |" for c in rep["preflight"]["checks"]]
    L += ["", "## Diagnosis", ""]
    L += [f"{i + 1}. **{f['title']}** (`{f['rule']}`, score {f['score']}){' — ' + f['label'] if f['label'] else ''}"
          for i, f in enumerate(rep["findings"])] or ["No findings."]
    tl = rep.get("three_layers")
    if tl:
        L += ["", "## Three layers", ""]
        for n in tl["nodes"]:
            L.append(f"- {n['node'] or 'no node inspected'}: **{n['status']}**" + "".join(
                f"\n  - {p['layer']} · {p['field']}: {p['have']} (want {p['want']})" for p in n["problems"]))
    L += ["", "## Security review", "", "| Area | Recorded | PRD gate | Note |", "|---|---|---|---|"]
    L += [f"| {r['area']} | {r['recorded']} | {'yes' if r.get('prd_gate') else ''} | {r['note']} |" for r in rep["security"]]
    L += ["", "## Open items", ""]
    L += [f"- [{'x' if o['recorded'] == 'done' else ' '}] {o['title']}{': ' + o['note'] if o['note'] else ''}" for o in rep["open_items"]]
    L += ["", "## Testing performed", ""] + [f"- {t}" for t in rep["testing_performed"]]
    L += ["", "## Not yet tested", ""] + [f"- {t['text']}" for t in rep["testing_not_performed"]]
    L += ["", "## Runs", "", "| Run | Kind | Name | Status | Changed state |", "|---|---|---|---|---|"]
    L += [f"| {r['id']} | {r['kind']} | {r['name']} | {r['status']} | {'yes' if r['changes_state'] else 'no'} |" for r in rep["runs"]]
    L += ["", "## Values file", "", "```yaml", rep["values_file"].rstrip(), "```", f"sha256 `{rep['values_sha256']}`", ""]
    return redact.text("\n".join(L))


def as_json(rep: dict) -> str:
    return json.dumps(rep, indent=2)
