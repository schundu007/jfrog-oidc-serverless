---
description: Read-only preflight + discovery for a real target (no changes)
argument-hint: <saved config name>
---

Using `.console/configs/$ARGUMENTS.json`, run only the read-only steps against
the real environment: Preflight and Discover (spec §4.1–4.2).

- Use the console's step modules, not ad-hoc commands, so the code paths get
  exercised.
- Report the tool, login and permission status, the discovered identity facts
  (SA `uniqueId` or MI principal/client IDs), kind corrections, and warnings.
- Show what the mapping claims JSON and the wire diff *would* be, without
  applying anything.

Do not run plan/apply, wire, rotation or probes. Those need my approval in the
console UI.
