"""Headless simulation.

    python -m console.sim list
    python -m console.sim run rotator-misses-3-runs           # human summary
    python -m console.sim run .console/configs/orders-prod.json --json
    python -m console.sim run happy-aca --set pull_path.ttl_seconds=1800 --seed 7
    python -m console.sim check-all                           # CI: built-ins vs expectations

Exit codes: run → 0 pass, 1 fail, 2 degraded; check-all → 0 when every
built-in meets its expectation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import scenario as sc
from .engine import Simulation


def _hhmm(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def _summary(result: dict) -> str:
    s, m = result["summary"], result.get("ttl_math") or {}
    lines = [
        f"{result['scenario']}  [{result['target_label']}]  verdict: {result['verdict'].upper()}",
        f"  rotations ok/failed  {s['rotations_ok']}/{s['rotations_failed']}",
        f"  pulls ok/failed      {s['pulls_ok']}/{s['pulls_failed']}  (cached {s['pulls_cached']})",
        f"  requests ok/cached/unavailable/failed  {s['requests_ok']}/{s['requests_ok_cached']}/"
        f"{s['requests_unavailable']}/{s['requests_failed']}",
        f"  unusable-credential exposure  {s['exposure_s'] / 60:.0f} min",
        f"  alert first fired    {_hhmm(s['alert_first_fired_s'])}",
    ]
    if m:
        lines.append(f"  TTL math             TTL {m['pull_ttl_seconds']}s, interval {m['rotation_interval_seconds']}s, "
                     f"pickup lag {m['pickup_lag_seconds'] or 0}s → survives {m['survives_missed_rotations']} missed rotation(s)")
    if result["chain"]:
        lines.append("  causal chain:")
        lines += [f"    → {c}" for c in result["chain"]]
    if result["root_cause"]:
        lines.append(f"  root cause: {result['root_cause']}")
    lines.append(f"  ({s['emulator_requests']} emulated HTTP calls, {s['elapsed_s']:.2f}s)")
    return "\n".join(lines)


def _parse_set(items) -> dict:
    out = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        if not key or not _:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m console.sim")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list built-in scenarios")
    run = sub.add_parser("run", help="run one scenario or saved config")
    run.add_argument("ref", help="built-in name, scenario .json, or console config .json")
    run.add_argument("--json", action="store_true", help="print the full result as JSON")
    run.add_argument("--seed", type=int)
    run.add_argument("--set", action="append", metavar="KEY=VALUE", help="config override, e.g. pull_path.ttl_seconds=1800")
    chk = sub.add_parser("check-all", help="run every built-in and compare with its expectation")
    chk.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "list":
        for s in sc.builtin_summaries():
            print(f"{s['name']:32} {s['target_kind']:26} expect {s['expect'].get('verdict')}")
        return 0

    if args.command == "run":
        try:
            scen = sc.load(args.ref, seed=args.seed, overrides=_parse_set(args.set))
        except (ValueError, OSError) as exc:
            print(f"console.sim: {exc}", file=sys.stderr)
            return 64
        result = Simulation(scen).run()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(_summary(result))
            problems = sc.check(result, scen["expect"]) if scen.get("expect") else []
            for p in problems:
                print(f"  ✗ expectation: {p}")
        return {"pass": 0, "fail": 1, "degraded": 2}[result["verdict"]]

    started = time.perf_counter()
    rows, bad = [], 0
    for name in sc.builtin_names():
        scen = sc.load(name)
        result = Simulation(scen).run()
        problems = sc.check(result, scen["expect"])
        bad += bool(problems)
        rows.append({"scenario": name, "verdict": result["verdict"], "expected": scen["expect"].get("verdict"),
                     "ok": not problems, "problems": problems, "seconds": result["summary"]["elapsed_s"]})
    if args.json:
        print(json.dumps({"ok": bad == 0, "results": rows}, indent=2))
    else:
        for r in rows:
            mark = "✓" if r["ok"] else "✗"
            print(f"{mark} {r['scenario']:32} {r['verdict']:9} (expected {r['expected']})  {r['seconds']:.2f}s")
            for p in r["problems"]:
                print(f"    {p}")
        print(f"{len(rows) - bad}/{len(rows)} scenarios meet expectations in {time.perf_counter() - started:.1f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
