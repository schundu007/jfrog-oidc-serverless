"""Run retention across the portal: only the newest KEEP runs are kept, live or archived, from every
console (Serverless, Kubernetes, Infrastructure and JFrog OIDC); every older finished run is deleted.
A run still going (running, waiting for approval, queued) is never deleted, and counts toward KEEP."""

from __future__ import annotations

import calendar
import time

KEEP = 7
ACTIVE = {"running", "waiting_approval", "prepared", "queued"}


def _epoch(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(calendar.timegm(time.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


def runs(serverless=None, k8s=None, jobs=None) -> list:
    """Every run as (started, active, delete): newest first."""
    out = []
    if serverless is not None:
        for r in serverless.runs.list():
            out.append((_epoch(r.get("created")), r.get("status") in ACTIVE, lambda i=r["id"]: serverless.runs.delete(i)))
        for e in serverless.archived()["entries"]:
            for r in e.get("run_rows") or []:
                out.append((_epoch(r.get("created")), False, lambda i=r["id"]: serverless.archive.drop_run(i)))
    if k8s is not None:
        for r in k8s.runs.list():
            out.append((_epoch(r.get("created")), r.get("status") in ACTIVE, lambda i=r["id"]: k8s.runs.delete(i)))
        for e in k8s.archived()["entries"]:
            for r in e.get("run_rows") or []:
                out.append((_epoch(r.get("created")), False, lambda i=r["id"]: k8s.archive.drop_run(i)))
    if jobs is not None:
        for r in jobs.history():
            out.append((_epoch(r.get("started")), r.get("state") == "running", lambda i=r["id"]: jobs.remove(i)))
        for r in jobs.archived():
            out.append((_epoch(r.get("started")), False, lambda i=r["id"]: jobs.remove_archived(i)))
    return sorted(out, key=lambda x: x[0], reverse=True)


def trim(serverless=None, k8s=None, jobs=None, keep: int = KEEP) -> int:
    """Delete every finished run past the newest `keep`. Returns how many were deleted."""
    n = 0
    for _, active, delete in runs(serverless, k8s, jobs)[keep:]:
        if active:
            continue
        try:
            delete()
            n += 1
        except Exception:  # noqa: BLE001 - a run that cannot go now goes on the next trim
            continue
    return n
