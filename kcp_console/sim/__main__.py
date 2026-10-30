"""python3 -m kcp_console.sim list | run <name> | check-all"""

from __future__ import annotations

import json
import sys

from . import engine


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        for n in engine.names():
            print(f"{n:42} {engine.load(n).get('title', '')}")
        return 0
    if cmd == "run" and len(argv) == 2:
        r = engine.run(engine.load(argv[1]))
        print(f"{r['name']}: {r['verdict']} (root cause: {r['root_cause']})")
        for line in r["chain"]:
            print(f"  {line}")
        for f in r["diagnosis"]["findings"]:
            print(f"  [{f['score']:3}] {f['rule']}: {f['title']}")
        if "--json" in argv:
            print(json.dumps({k: r[k] for k in ("verdict", "root_cause", "chain")}, indent=2))
        return 0
    if cmd == "check-all":
        bad = 0
        for n in engine.names():
            problems = engine.check(engine.load(n))
            print(f"{'ok  ' if not problems else 'FAIL'} {n}")
            for p in problems:
                print(f"     {p}")
            bad += bool(problems)
        print(f"{len(engine.names()) - bad}/{len(engine.names())} scenarios match their expectations")
        return 1 if bad else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
