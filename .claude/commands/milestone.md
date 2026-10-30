---
description: Implement one console milestone from docs/console-spec.md (e.g. /milestone M1)
argument-hint: M0|M1|M2|M3|M4|M5|M6
---

Implement milestone $ARGUMENTS of the console.

1. Read `CLAUDE.md`, the milestone's section of `docs/console-spec.md` §9, and
   every spec section it references. Read `docs/progress.md` to see what is done.
2. Write a short plan in the task list: files to add or change, and the tests
   that prove each acceptance criterion.
3. Work test-first. Add failing tests for the acceptance criteria, then
   implement until they pass. Keep to the Rules in `CLAUDE.md`: stdlib only, no
   token output, real code in simulation, argv-only subprocesses.
4. Run the full test suite, not just the new tests:
   client, rotator, console, and `python -m console.sim check-all` from M1 on.
5. If the milestone touches Terraform, run `terraform fmt -recursive -check` and
   `terraform validate` on any workspace you generate.
6. Append to `docs/progress.md`: the milestone, what was built, how to demo it,
   test counts, and anything left open or deferred, with the reason.
7. Stop and summarise. Do not start the next milestone.

Never run approval-gated commands (`terraform apply`, cloud update/deploy/start,
JFrog writes) against real accounts unless I explicitly ask in this session.
