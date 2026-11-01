"""The workload's own live log around a Prove check, read-only.

Each sample app prints one `jfrog_oidc_selftest` JSON line per self-test (jfrog_oidc.probe.log_result):
what JFrog answered to the workload's identity, in words, never a token. This reads the platform's log
for the proof window: Container Apps console log (streamed from the replica, no ingestion delay) or
Cloud Logging. Every line goes through redact before it is kept.
"""

from __future__ import annotations

import json
import re
import time

from .. import model, redact
from .verify import _a, _g

EVENT = "jfrog_oidc_selftest"
TRIES, WAIT = 4, 10
sleep = time.sleep   # seam for tests

_ACA = ("azure_container_app",)
_GCP = {"gcp_run_service": ("cloud_run_revision", "service_name"),
        "gcp_run_function": ("cloud_run_revision", "service_name"),
        "gcp_run_job": ("cloud_run_job", "job_name"),
        "gcp_run_worker_pool": ("cloud_run_worker_pool", "worker_pool_name")}
_BAD = re.compile(r"\b[45]\d\d\s*$|Traceback|Error\b|refused|Forbidden", re.I)


def supported(cfg) -> bool:
    k = model.get_path(cfg, "target.kind")
    return k in _ACA or k in _GCP


def argv(cfg, since: str = ""):
    kind = model.get_path(cfg, "target.kind")
    if kind in _ACA:
        n, rg, s = _a(cfg)
        return ["az", "containerapp", "logs", "show", "-g", rg, "-n", n, "--subscription", s, "--type", "console",
                "--tail", "300", "--format", "json", "--only-show-errors"]
    if kind in _GCP:
        n, _, p = _g(cfg)
        rtype, label = _GCP[kind]
        f = f'resource.type="{rtype}" AND resource.labels.{label}="{n}" AND timestamp>="{since}"'
        return ["gcloud", "logging", "read", f, "--project", p, "--order=asc", "--limit=200", "--format=json"]
    return None


def _at(ts: str) -> str:
    return (ts or "")[:19] + "Z" if ts else ""


def _row(at: str, raw, severity: str = "") -> dict:
    ev = raw if isinstance(raw, dict) else None
    if ev is None and isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            ev = json.loads(raw)
        except ValueError:
            ev = None
    if isinstance(ev, dict) and ev.get("event") == EVENT:
        msg, bad = ev.get("message") or "", not ev.get("ok")
    elif isinstance(ev, dict):
        msg, ev = ev.get("message") or json.dumps(ev), None
        bad = bool(_BAD.search(msg))
    else:
        msg = str(raw or "")
        bad = bool(_BAD.search(msg))
    if severity in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
        bad = True
    row = {"at": at, "level": "ERROR" if bad else "INFO", "message": redact.text(msg.strip())[:600]}
    return row, ev


def _parse_aca(out: str):
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        log = re.sub(r"^[FP] ", "", str(e.get("Log") or ""))
        yield _at(e.get("TimeStamp")), log, ""


def _parse_gcp(out: str):
    try:
        entries = json.loads(out or "[]")
    except ValueError:
        return
    for e in entries or []:
        if "jsonPayload" in e:
            raw = e["jsonPayload"]
        elif "textPayload" in e:
            raw = e["textPayload"]
        elif "httpRequest" in e:
            h = e["httpRequest"]
            raw = f"{h.get('requestMethod', '')} {(h.get('requestUrl') or '').split('?')[0]} {h.get('status', '')}"
        else:
            continue
        yield _at(e.get("timestamp")), raw, e.get("severity") or ""


def read(cfg, sh, since: str):
    """{source, since, rows: [{at, level, message}], jfrog: last answer, answers: [...]} or None."""
    cmd = argv(cfg, since)
    if not cmd:
        return None
    gcp = cmd[0] == "gcloud"
    got = None
    for i in range(TRIES):
        r = sh.run(cmd, timeout=90)
        if r.ok:
            rows, answers = [], []
            for at, raw, sev in (_parse_gcp if gcp else _parse_aca)(r.stdout):
                if not at or at < since:
                    continue
                row, ev = _row(at, raw, sev)
                if not row["message"]:
                    continue
                rows.append(row)
                if ev:
                    answers.append({"status": ev.get("jfrog_status"), "answer": row["message"],
                                    "provider": ev.get("provider"), "subject": ev.get("sub"), "at": at})
            got = {"source": "Cloud Logging (live)" if gcp else "Container Apps console log (live)",
                   "workload": model.get_path(cfg, f"target.{'gcp' if gcp else 'azure'}.name"), "since": since,
                   "rows": rows[-200:], "answers": answers, "jfrog": answers[-1] if answers else None}
            if answers:
                return got
        if i < TRIES - 1:
            sleep(WAIT)
    return got


def answer_from_check(res: dict) -> dict:
    """JFrog's answer as the console's check saw it: HTTP status and message, never a token."""
    if (res or {}).get("status") == "pass":
        msg = f"token issued for {res.get('jfrog_user') or 'the mapped user'}"
        if res.get("token_lifetime"):
            msg += f", expires in {res['token_lifetime']}"
        return {"status": 200, "answer": msg}
    reason = str((res or {}).get("reason") or "")
    m = re.search(r"HTTP (\d{3})", reason)
    msgs = re.findall(r'"message"\s*:\s*"([^"]*)"', reason)
    answer = "; ".join(msgs) if msgs else re.sub(r"^exchange failed:\s*", "", reason)
    return {"status": int(m.group(1)) if m else None, "answer": redact.text(answer)[:400]}
