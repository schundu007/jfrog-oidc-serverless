"""The fleet view: one row per saved cluster, from the stored status (the last read) and the run records.
No live calls, so a fleet of hundreds of clusters lists at once; a cluster is read live when it is opened.

state, most urgent first:
  approval       a real run waits for its approval
  running        a real run is under way: its result replaces the last proof when it ends
  attention      the last read found a failing check
  not_proven     installed, and the newest real Set up or Verify did not prove OIDC
  unverified     installed, never proven
  working        the newest real Set up or Verify proved OIDC end to end
  not_installed  read, no credential provider release
  unread         never read
"""

from __future__ import annotations

from . import model

PROOF = ("cluster", "sa-check", "trust", "map", "mapping", "provider", "anon", "pull", "who")
ORDER = ("approval", "running", "attention", "not_proven", "unverified", "working", "not_installed", "unread")


def _real(r):
    return (r.get("request") or {}).get("parameters", {}).get("DryRun") is False


def _proof(runs):
    """The newest real Set up or Verify that reached the pull test: working, not_proven, or None."""
    for r in runs:
        if r.get("kind") != "direct" or r.get("name") == "teardown" or not _real(r):
            continue
        steps = (r.get("result") or {}).get("steps") or []
        if not any(s.get("id") == "pull" for s in steps):
            continue
        proven = (r.get("status") == "succeeded" and any(s.get("id") == "who" and s.get("status") == "ok" for s in steps)
                  and not any(s.get("status") in ("failed", "warn") for s in steps if s.get("id") in PROOF))
        return ("working" if proven else "not_proven"), r.get("created", "")
    return None, ""


def row(c: dict, st: dict, runs: list) -> dict:
    """c: a store.list() entry; st: its status file; runs: every run, newest first."""
    mine = [r for r in runs if r.get("config") == c["name"]]
    d = st.get("discover")
    rel = (d or {}).get("release") or None
    ds = next((x for x in (d or {}).get("daemonsets") or [] if x.get("jfrog")), None)
    checks = (st.get("preflight") or {}).get("checks") or []
    bad = next((x for x in checks if x.get("status") == "fail"), None)
    oidc, verified_at = _proof(mine)
    act = next((r for r in mine if r.get("kind") == "direct" and r.get("name") in ("setup", "verify", "teardown")), None)
    if act and _real(act) and act.get("status") == "prepared":
        state = "approval"
    elif act and _real(act) and act.get("status") in ("queued", "running"):
        state = "running"
    elif d is None:
        state = "unread"
    elif bad:
        state = "attention"
    elif not rel:
        state = "not_installed"
    else:
        state = oidc or "unverified"
    chart = (rel or {}).get("chart", "")
    return {
        "name": c["name"], "cluster": c.get("cluster", ""), "cloud": c.get("platform") or "aks",
        "subscription": c.get("subscription", ""), "resource_group": c.get("resource_group", ""),
        "location": ((d or {}).get("cluster") or {}).get("location", ""),
        "kubernetes": ((d or {}).get("cluster") or {}).get("kubernetes_version", ""),
        "state": state, "oidc": oidc or "unchecked", "verified_at": verified_at, "problem": (bad or {}).get("title", ""),
        "chart": chart.rsplit("-", 1)[-1] if chart else "", "nodes": len((d or {}).get("nodes") or []),
        "ready": f"{ds.get('ready', 0)}/{ds.get('desired', 0)}" if ds else "",
        "read_at": st.get("discover_at", ""), "updated": c.get("updated", ""),
        "action": {"which": act["name"], "status": act["status"], "dry": not _real(act), "at": act.get("created", "")} if act else None,
    }


def rows(configs: list, statuses: dict, runs: list) -> list:
    out = [row(c, statuses.get(c["name"]) or {}, runs) for c in configs]
    return sorted(out, key=lambda r: (ORDER.index(r["state"]), r["name"]))


def counts(rs: list) -> dict:
    out = {"all": len(rs)}
    for r in rs:
        out[r["state"]] = out.get(r["state"], 0) + 1
    return out


def platforms() -> dict:
    return dict(model.PLATFORMS)
