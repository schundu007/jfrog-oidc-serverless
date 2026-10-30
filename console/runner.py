"""Pipeline runner (spec §4): the 9 steps, one worker thread per run.

Step states: pending → running → succeeded | failed | needs_approval | skipped | cancelled.
Run states:  running | waiting_approval | succeeded | failed | cancelled.

Gates:
- apply waits for an approval of the exact plan (sha256, plus the typed
  workspace name for any delete/replace). The check runs when the operator
  approves, so a refusal is returned to them at once and the run keeps waiting.
- wire waits for an approval of the exact diff. If the target changed before
  the commands ran, the new diff is shown and the run waits again.
Nothing is applied without its approval, and a cancelled run applies nothing.

Every event is redacted and appended to <root>/runs/<id>/events.jsonl. First
rotation, Verify and Report arrive in M5 and are skipped for now.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import deliver, model, redact
from .steps import discover, preflight, tfgen
from .steps import terraform as tf
from .steps import wire as wiremod

STEPS = ["preflight", "discover", "simulate", "publish", "plan", "apply", "wire", "first_rotation", "verify", "report"]
DEFAULT_STEPS = ["preflight", "discover", "simulate", "plan", "apply", "wire"]
DELIVERY_STEPS = ["preflight", "discover", "simulate", "publish"]
LOCAL_ONLY = {"plan", "apply", "wire"}
LATER = {"first_rotation", "verify", "report"}
TITLES = {"preflight": "Preflight", "discover": "Discover", "simulate": "Simulate", "publish": "Deliver", "plan": "Plan", "apply": "Apply",
          "wire": "Wire target", "first_rotation": "First rotation", "verify": "Verify", "report": "Report"}
MAX_RUNS_KEPT = 50
EXPECTED_ERRORS = (ValueError, RuntimeError, TimeoutError, PermissionError, OSError)


class RunError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


class Cancelled(Exception):
    pass


class _Waiting:
    def __init__(self, step, info, check):
        self.step, self.info, self.check = step, info, check
        self.payload = None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Run:
    def __init__(self, cfg: dict, steps: list, root: Path, hidden: dict | None = None):
        self.id = time.strftime("%Y%m%d-%H%M%S-", time.gmtime()) + uuid.uuid4().hex[:6]
        self.cfg = copy.deepcopy(cfg)
        self.name = cfg.get("name")
        self.created = _now()
        self.status = "running"
        self.cond = threading.Condition()
        self.events: list = []
        self.cancelled = False
        self.waiting: _Waiting | None = None
        self.current = None
        self.steps = {s: {"name": s, "title": TITLES[s], "status": "pending" if s in steps else "skipped",
                          "started": None, "finished": None, "evidence": {}, "error": None, "approval": None,
                          "note": None if s in steps else "not requested in this run", "planned": False}
                      for s in STEPS}
        for s in LATER:
            self.steps[s].update(status="skipped", planned=True,
                                 note="arrives in M5 (first rotation, live probes, ready report)")
        for s, note in (hidden or {}).items():
            self.steps[s].update(status="skipped", planned=True, note=note)
        self.dir = root / "runs" / self.id
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._events_file = self.dir / "events.jsonl"
        # Name and kind only: enough to list the run again if the console restarts before it ends.
        meta = self.dir / "config.json"
        meta.write_text(json.dumps({"name": self.name, "target": {"kind": model.get_path(cfg, "target.kind")}}) + "\n")
        os.chmod(meta, 0o600)

    @classmethod
    def restore(cls, d: Path) -> "Run | None":
        """A run of an earlier console process, read back from its folder (None when unreadable).
        A finished run comes from run.json; one cut off by a restart is rebuilt from its step
        events and ends as cancelled, since its worker thread is gone."""
        try:
            lines = (d / "events.jsonl").read_text().splitlines() if (d / "events.jsonl").exists() else []
            events = [json.loads(line) for line in lines if line.strip()]
            snap = json.loads((d / "run.json").read_text()) if (d / "run.json").exists() else None
            meta = json.loads((d / "config.json").read_text()) if (d / "config.json").exists() else {}
        except (OSError, ValueError):
            return None
        if not isinstance(snap, (dict, type(None))) or (snap is None and not events):
            return None
        run = cls.__new__(cls)
        run.id, run.dir, run._events_file = d.name, d, d / "events.jsonl"
        run.cond, run.events, run.cancelled, run.waiting, run.current = threading.Condition(), events, False, None, None
        run.steps = {s: {"name": s, "title": TITLES[s], "status": "skipped", "started": None, "finished": None, "evidence": {},
                         "error": None, "approval": None, "note": None, "planned": False} for s in STEPS}
        if snap:
            run.name, kind, run.status, run.created = snap.get("name"), snap.get("kind"), snap.get("status"), snap.get("created")
            for st in snap.get("steps") or []:
                if st.get("name") in run.steps:
                    run.steps[st["name"]] = st
        else:
            run.name, kind, run.status = meta.get("name"), model.get_path(meta, "target.kind"), "cancelled"
            run.created = events[0].get("t")
            for ev in events:
                if ev.get("kind") == "step" and ev.get("name") in run.steps:
                    run.steps[ev["name"]].update(status=ev.get("status"), error=ev.get("error"), note=ev.get("note"))
            for st in run.steps.values():
                if st["status"] in ("running", "needs_approval", "pending"):
                    st.update(status="cancelled", note=f"the console restarted while this step was {st['status'].replace('_', ' ')}")
        run.cfg = {"name": run.name, "target": {"kind": kind}}
        return run

    # -- events ------------------------------------------------------------------------

    def emit(self, kind: str, **data) -> None:
        with self.cond:
            ev = redact.obj({"seq": len(self.events), "t": _now(), "kind": kind, "step": self.current, **data})
            self.events.append(ev)
            fd = os.open(self._events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self.cond.notify_all()

    def log(self, line: str) -> None:
        self.emit("log", line=redact.text(str(line)))

    def set_step(self, name: str, **fields) -> None:
        with self.cond:
            self.steps[name].update(fields)
            st = dict(self.steps[name])
        self.emit("step", name=name, status=st["status"], error=st["error"], note=st["note"])

    def snapshot(self) -> dict:
        with self.cond:
            return redact.obj({
                "id": self.id, "name": self.name, "kind": model.get_path(self.cfg, "target.kind"), "status": self.status,
                "created": self.created, "current": self.current, "events": len(self.events),
                "steps": [copy.deepcopy(self.steps[s]) for s in STEPS],
            })

    def summary(self) -> dict:
        with self.cond:
            return {"id": self.id, "name": self.name, "kind": model.get_path(self.cfg, "target.kind"),
                    "status": self.status, "created": self.created, "current": self.current}


class RunManager:
    def __init__(self, root, cloud_shell, tf_shell, env=None, simulate=None, store=None, delivery=None, repo_root=None):
        self.root = Path(root)
        self.cloud_shell, self.tf_shell = cloud_shell, tf_shell
        self.env = os.environ if env is None else env
        self.simulate = simulate
        self.store = store
        # With delivery settings (pipelines/delivery.json) a run proposes the change as an
        # Azure DevOps pull request; the <cloud>-workload-onboard pipeline plans, applies and wires.
        self.delivery = delivery
        self.repo_root = Path(repo_root) if repo_root else None
        self.runs: dict = self._load()
        self.lock = threading.Lock()
        self.threads: list = []

    def _load(self) -> dict:
        """The runs saved under <root>/runs, oldest first, so the list survives a restart."""
        folders = sorted(p for p in (self.root / "runs").glob("*") if p.is_dir()) if (self.root / "runs").is_dir() else []
        runs = (Run.restore(p) for p in folders[-MAX_RUNS_KEPT:])
        return {r.id: r for r in runs if r is not None}

    # -- API ---------------------------------------------------------------------------

    def default_steps(self) -> list:
        return list(DELIVERY_STEPS if self.delivery else DEFAULT_STEPS)

    def start(self, cfg: dict, steps=None) -> Run:
        steps = list(steps or self.default_steps())
        bad = [s for s in steps if s not in STEPS]
        if bad:
            raise RunError(400, "unknown_step", f"unknown step(s): {', '.join(map(str, bad))}")
        if self.delivery and LOCAL_ONLY & set(steps):
            raise RunError(400, "delivered_by_pipeline",
                           "plan, apply and wire run in the <cloud>-workload-onboard pipeline; deliver instead")
        if not self.delivery and "publish" in steps:
            raise RunError(400, "no_delivery", "publishing needs pipelines/delivery.json")
        if "apply" in steps and "plan" not in steps:
            raise RunError(400, "apply_needs_plan", "apply only runs a plan made in the same run; include 'plan'")
        errors = model.validate(cfg)["errors"]
        if errors:
            raise RunError(400, "invalid_config", "; ".join(f"{e['path']}: {e['message']}" for e in errors))
        with self.lock:
            busy = [r for r in self.runs.values() if r.name == cfg.get("name") and r.status in ("running", "waiting_approval")]
            if busy:
                raise RunError(409, "run_active", f"run {busy[0].id} for {cfg.get('name')} is still active; finish or cancel it")
            hidden = ({s: "runs in the <cloud>-workload-onboard pipeline after the pull request merges" for s in LOCAL_ONLY}
                      if self.delivery else {"publish": "local mode: no pipelines/delivery.json"})
            run = Run(cfg, steps, self.root, hidden)
            self.runs[run.id] = run
            self._trim()
        t = threading.Thread(target=self._work, args=(run,), name=f"run-{run.id}", daemon=True)
        self.threads.append(t)
        t.start()
        return run

    def get(self, run_id: str) -> Run:
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            raise RunError(404, "no_such_run", "no such run")
        return run

    def list(self) -> list:
        with self.lock:
            runs = list(self.runs.values())
        return [r.summary() for r in reversed(runs)]

    def delete_for(self, name: str) -> dict:
        """Forget the finished runs of a deleted setup and remove their folders. Active
        runs (running, waiting for approval) are kept: they may be mid-change."""
        deleted, kept = [], 0
        with self.lock:
            for rid, run in list(self.runs.items()):
                if run.name != name:
                    continue
                if run.status in ("running", "waiting_approval"):
                    kept += 1
                    continue
                deleted.append(self.runs.pop(rid))
        for run in deleted:
            shutil.rmtree(run.dir, ignore_errors=True)
        return {"runs_deleted": len(deleted), "runs_kept": kept}

    def approve(self, run_id: str, step: str, payload: dict) -> dict:
        run = self.get(run_id)
        with run.cond:
            w = run.waiting
            if run.status != "waiting_approval" or w is None or w.step != step:
                raise RunError(409, "not_waiting", f"the run is not waiting for an approval of '{step}'")
            payload = dict(payload) if isinstance(payload, dict) else {}
            try:
                w.check(payload)
            except (tf.ApplyRefused, wiremod.WireRefused) as exc:
                raise RunError(409, exc.code, str(exc)) from None
            w.payload = payload
            # Leave the waiting state now, not when the worker wakes, so no
            # reader sees an already-approved step as still waiting.
            run.waiting = None
            run.status = "running"
            run.steps[step]["status"] = "running"
            run.cond.notify_all()
        run.emit("approved", name=step)
        return {"accepted": True, "step": step}

    def cancel(self, run_id: str) -> dict:
        run = self.get(run_id)
        with run.cond:
            if run.status not in ("running", "waiting_approval"):
                raise RunError(409, "not_active", "the run has finished")
            run.cancelled = True
            run.cond.notify_all()
        run.emit("cancel_requested")
        return {"cancelled": True}

    def shutdown(self) -> None:
        with self.lock:
            runs = list(self.runs.values())
        for r in runs:
            with r.cond:
                r.cancelled = True
                r.cond.notify_all()
        for t in self.threads:
            t.join(timeout=5)

    def _trim(self):
        while len(self.runs) > MAX_RUNS_KEPT:
            oldest = next((k for k, r in self.runs.items() if r.status not in ("running", "waiting_approval")), None)
            if oldest is None:
                return
            del self.runs[oldest]

    # -- worker ------------------------------------------------------------------------------

    def _work(self, run: Run) -> None:
        ctx = {"cloud": None, "tf_sh": None, "tf": None, "plan": None, "outputs": None}
        final = "succeeded"
        try:
            for name in STEPS:
                if run.steps[name]["status"] != "pending":
                    continue
                if run.cancelled:
                    raise Cancelled()
                run.current = name
                run.set_step(name, status="running", started=_now())
                try:
                    getattr(self, f"_step_{name}")(run, ctx)
                except Cancelled:
                    run.set_step(name, status="cancelled", finished=_now())
                    raise
                except Exception as exc:  # noqa: BLE001 - every failure ends the step with its message
                    msg = str(exc) if isinstance(exc, EXPECTED_ERRORS) else f"{type(exc).__name__}: {exc}"
                    run.set_step(name, status="failed", finished=_now(), error=redact.text(msg))
                    final = "failed"
                    break
                if run.steps[name]["status"] == "running":
                    run.set_step(name, status="succeeded", finished=_now())
                elif run.steps[name]["finished"] is None:
                    run.set_step(name, finished=_now())
        except Cancelled:
            final = "cancelled"
        with run.cond:
            run.status, run.current, run.waiting = final, None, None
            run.cond.notify_all()
        run.emit("done", status=final)
        p = run.dir / "run.json"
        p.write_text(json.dumps(run.snapshot(), indent=2) + "\n")
        os.chmod(p, 0o600)

    def _shell(self, run, ctx, which="cloud"):
        key = "cloud" if which == "cloud" else "tf_sh"
        if ctx[key] is None:
            ctx[key] = (self.cloud_shell if which == "cloud" else self.tf_shell)(run.log)
        return ctx[key]

    def _evidence(self, run, name, **ev):
        with run.cond:
            run.steps[name]["evidence"].update(redact.obj(ev))
        run.emit("evidence", name=name, keys=sorted(ev))

    def _wait_approval(self, run: Run, step: str, info: dict, check) -> dict:
        w = _Waiting(step, info, check)
        with run.cond:
            run.waiting = w
            run.status = "waiting_approval"
            run.steps[step].update(status="needs_approval", approval=redact.obj(info))
        run.emit("approval_required", name=step, approval=info)
        with run.cond:
            while w.payload is None and not run.cancelled:
                run.cond.wait(1.0)
            run.waiting = None
            if run.cancelled:
                raise Cancelled()
            run.status = "running"
            run.steps[step]["status"] = "running"
            return w.payload

    # -- steps ----------------------------------------------------------------------------------

    def _step_preflight(self, run, ctx):
        res = preflight.run(run.cfg, self._shell(run, ctx), env=self.env, delivery=self.delivery or False,
                            repo_root=self.repo_root)
        self._evidence(run, "preflight", result=res)
        for path, value in (res.get("config_patch") or {}).items():
            model.set_path(run.cfg, path, value)
        if res["status"] == "fail":
            bad = [c["title"] for c in res["checks"] if c["status"] == "fail"]
            raise RuntimeError("preflight failed: " + "; ".join(bad))

    def _step_discover(self, run, ctx):
        res = discover.run(run.cfg, self._shell(run, ctx))
        self._evidence(run, "discover", result=res)
        if res["errors"]:
            raise RuntimeError("discover failed: " + "; ".join(e["message"] for e in res["errors"]))
        for path, value in res["config_patch"].items():
            model.set_path(run.cfg, path, value)
        if self.store is not None:
            try:
                self.store.get(run.name)
            except FileNotFoundError:
                return
            self.store.put(run.name, run.cfg)
            run.log(f"saved the discovered facts to config {run.name}")

    def _step_simulate(self, run, ctx):
        if self.simulate is None:
            run.set_step("simulate", status="skipped", note="the simulator is not available in this process")
            return
        result = self.simulate(run.cfg) or {}
        verdict = result.get("verdict") or {}
        self._evidence(run, "simulate", verdict=verdict, summary=result.get("summary"))
        if verdict.get("status") != "pass":
            # Spec §4: a failing simulation warns; it does not block the plan.
            run.set_step("simulate", note="the simulation found failing periods; review them before approving the plan")

    def _step_publish(self, run, ctx):
        res = deliver.publish(run.cfg, self._shell(run, ctx), self.repo_root, self.delivery, run.id, log=run.log)
        self._evidence(run, "publish", result=res)


    def _step_plan(self, run, ctx):
        rendered = tfgen.render(run.cfg, self.root, on_wait=run.log)
        for w in rendered["warnings"]:
            run.log(f"warning: {w}")
        ctx["tf"] = tf.Terraform(self._shell(run, ctx, "tf"), rendered["dir"], env=self.env, on_wait=run.log)
        plan = ctx["tf"].plan()
        ctx["plan"] = plan
        self._evidence(run, "plan", workspace={"workspace_key": rendered["key"], "dir": rendered["dir"],
                                               "targets": rendered["targets"], "warnings": rendered["warnings"]}, plan=plan)

    def _step_apply(self, run, ctx):
        t, plan = ctx["tf"], ctx["plan"]
        s = plan["summary"]
        if not any(s.get(k) for k in ("create", "update", "replace", "delete")):
            ctx["outputs"] = t.outputs()
            self._evidence(run, "apply", result={"applied": False, "reason": "the plan has no changes"},
                           outputs=ctx["outputs"])
            return
        info = {"plan_sha256": plan["sha256"], "summary": s, "destructive": plan["destructive"],
                "confirmation_phrase": plan["confirmation_phrase"], "workspace": plan["workspace"]}
        payload = self._wait_approval(run, "apply", info, lambda p: t.check_approval(plan, p))
        res = t.apply(plan, payload)
        ctx["outputs"] = res["outputs"]
        self._evidence(run, "apply", result={"applied": True, "sha256": res["sha256"]}, outputs=res["outputs"])

    def _step_wire(self, run, ctx):
        outputs = ctx["outputs"]
        if outputs is None:
            ws = tfgen.workspace_dir(self.root, tfgen.workspace_key(run.cfg))
            if not ws.is_dir():
                raise RuntimeError("no workspace for this target yet: run Plan and Apply first")
            outputs = tf.Terraform(self._shell(run, ctx, "tf"), ws, env=self.env, on_wait=run.log).outputs()
        w = wiremod.Wire(run.cfg, self._shell(run, ctx), outputs)
        while True:
            plan = w.plan()
            self._evidence(run, "wire", diff=plan)
            if plan["noop"]:
                self._evidence(run, "wire", result={"applied": False, "verified": True, "reason": "already wired"})
                return

            def check(p, plan=plan):
                if not p.get("diff_sha256"):
                    raise wiremod.WireRefused("not_approved", "wiring needs the operator's approval of the shown diff")
                if p["diff_sha256"] != plan["sha256"]:
                    raise wiremod.WireRefused("diff_hash_mismatch", "the approval is for a different diff; review the current one")
            payload = self._wait_approval(run, "wire", {"diff_sha256": plan["sha256"], "target": plan["target"]}, check)
            try:
                res = w.apply(plan, payload)
            except wiremod.WireRefused as exc:
                if exc.code != "target_changed":
                    raise
                run.log("the target changed since the diff was shown; showing the new diff")
                continue
            self._evidence(run, "wire", result=res)
            if not res["verified"]:
                raise RuntimeError("the commands ran but the target does not match the desired settings; see the diff")
            return
