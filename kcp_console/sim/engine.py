"""Runs a scenario's steps on the World, then the console's own checks and rule
engine on the evidence the run produced (spec §8).

A scenario is JSON: name, title, story, runbook (the section it reproduces),
config (overrides on the sandbox defaults), steps ([op, {args}] pairs) and expect
(verdict, root_cause, findings that must appear, chain fragments that must appear).
"""

from __future__ import annotations

import json
import pathlib

from .. import diagnose
from .world import World

SCENARIOS = pathlib.Path(__file__).with_name("scenarios")

# World methods a scenario may call. Anything else is refused.
OPS = {"mirror_binary", "create_provider", "create_mapping", "foreign_provider", "grant", "install",
       "restart_daemonset", "add_orphan", "delete_orphans", "replace_node", "add_node", "reset_node_entries",
       "create_pod", "remove_image", "restart_kubelet", "annotate_sa", "check_kubelet_provider", "check_pod_config",
       "probe_realm", "promote", "aql", "pull_remote", "advance"}


def baseline(w: World, mirror=None, mapping=None, promote=True):
    """Phases 3 and 3b as the pilot did them: binary mirrored, provider and mapping created, test tag promoted."""
    w.mirror_binary(**(mirror or {}))
    w.create_provider()
    w.create_mapping(**(mapping or {}))
    if promote:
        j = w.cfg["jfrog"]
        w.promote(j["promote_source_repo"], j["test_repo"], "mcp/sonarqube", "1.26-v1")


def set_flag(w: World, setup_restarts_kubelet=None):
    if setup_restarts_kubelet is not None:
        w.setup_restarts_kubelet = bool(setup_restarts_kubelet)


MACROS = {"baseline": baseline, "set": set_flag}


def load(name: str) -> dict:
    path = SCENARIOS / f"{name}.json"
    if not path.is_file() or path.parent != SCENARIOS:
        raise KeyError(f"unknown scenario {name}")
    return json.loads(path.read_text())


def names() -> list:
    return sorted(p.stem for p in SCENARIOS.glob("*.json"))


def run(scenario: dict) -> dict:
    w = World(scenario.get("config") or {})
    for step in scenario.get("steps", []):
        op, args = step[0], (step[1] if len(step) > 1 else {})
        if op in MACROS:
            MACROS[op](w, **args)
        elif op in OPS:
            getattr(w, op)(**args)
        else:
            raise ValueError(f"scenario {scenario.get('name')}: unknown op {op}")
    bundle = w.bundle()
    d = diagnose.diagnose(bundle)
    return {"name": scenario.get("name"), "title": scenario.get("title"), "story": scenario.get("story"),
            "runbook": scenario.get("runbook"), "verdict": d["verdict"]["status"],
            "root_cause": d["verdict"]["root_cause"], "diagnosis": d, "chain": list(w.chain),
            "events": list(w.events), "bundle": bundle}


def check(scenario: dict, result: dict | None = None) -> list:
    """Mismatches between a run and the scenario's expectations; empty means pass."""
    r = result or run(scenario)
    exp = scenario.get("expect") or {}
    out = []
    if "verdict" in exp and r["verdict"] != exp["verdict"]:
        out.append(f"verdict {r['verdict']} != {exp['verdict']}")
    if "root_cause" in exp and r["root_cause"] != exp["root_cause"]:
        out.append(f"root_cause {r['root_cause']} != {exp['root_cause']}")
    rules = {f["rule"] for f in r["diagnosis"]["findings"]}
    for f in exp.get("findings", []):
        if f not in rules:
            out.append(f"missing finding {f} (have {sorted(rules)})")
    for f in exp.get("absent", []):
        if f in rules:
            out.append(f"unexpected finding {f}")
    chain = "\n".join(r["chain"])
    for frag in exp.get("chain", []):
        if frag not in chain:
            out.append(f"chain lacks {frag!r}")
    if "control_refused" in exp:
        c = r["diagnosis"]["verdict"]["control"]
        if not c or c["refused"] != exp["control_refused"]:
            out.append(f"control refused {c and c['refused']} != {exp['control_refused']}")
    if "audit" in exp and r["diagnosis"]["verdict"]["audit"]["status"] != exp["audit"]:
        out.append(f"audit {r['diagnosis']['verdict']['audit']['status']} != {exp['audit']}")
    return out
