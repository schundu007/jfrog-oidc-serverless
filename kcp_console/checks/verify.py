"""Verify: the pull verdict, the fresh-exchange note, kubelet_restart and audit attribution."""

from __future__ import annotations

import datetime as dt

from .. import diagnose, logic
from ..parse import summary


def kubelet_restart(kubelet_check_text: str) -> list:
    out = []
    for n in summary.quick_summary(kubelet_check_text)["nodes"]:
        older = n.get("kubelet_older_than_config")
        out.append({"node": n["node"], "kubelet_started": n["kubelet_started"], "config_mtime": n["config_mtime"],
                    "status": "fail" if older else "pass" if older is False else "unknown",
                    "detail": "kubelet is running an older config: it started before the file was last written" if older
                    else "kubelet started after the last write" if older is False else "times not in the output"})
    return out


def verify(pod: dict, kubelet_check_text: str = "") -> dict:
    return diagnose.pod_verdict(pod, kubelet_check_text)


def pull_window(pod: dict, minutes: int = 5) -> dict:
    """The audit query window around the pod's pull."""
    t = logic.normalize_ts(pod.get("time") or "")
    try:
        c = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return {"from": None, "to": None}
    f = lambda x: x.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"from": f(c - dt.timedelta(minutes=minutes)), "to": f(c + dt.timedelta(minutes=1))}


def parse_request_log(text: str) -> list:
    """artifactory-request.log lines: date|trace|ip|user|method|path|status|... (pipe-separated)."""
    out = []
    for line in (text or "").splitlines():
        p = [x.strip() for x in line.split("|")]
        if len(p) >= 7 and p[4] in ("GET", "HEAD"):
            ts = p[0].replace(" ", "T")[:19] + "Z"
            try:
                status = int(p[6])
            except ValueError:
                continue
            out.append({"t": ts, "service": "artifactory", "user": p[3], "method": p[4], "path": p[5], "status": status})
    return out


def audit_attribution(records: list, mapped_user: str, window: dict | None = None) -> dict:
    if window and window.get("from"):
        records = [r for r in records if window["from"] <= r.get("t", "") <= window["to"]]
    return diagnose.attribution(records, mapped_user)
