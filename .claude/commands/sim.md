---
description: Run simulation scenarios headless and explain the results
argument-hint: [scenario-name | path/to/config.json | all]
---

Run the console simulator for: $ARGUMENTS (default: all).

- `all` → `PYTHONPATH=client/src:rotator:. python -m console.sim check-all`
- a scenario or config → `PYTHONPATH=client/src:rotator:. python -m console.sim run <arg> --json`

Report:
- The verdict per scenario and whether it matches the expectation.
- For any failing or degraded run, the causal chain the engine reports.
- For a saved config: how many missed rotations it survives, the platform pickup
  lag assumed, and the TTL or cron change that would make it pass. Re-run with
  that override to confirm before suggesting it.

Do not change scenario expectations to make a run pass. If an expectation looks
wrong, explain why and ask.
