"""Artifactory-side checks (spec §6): orphaned mappings, binary integrity,
token scope, the realm probe and dormant remotes. Pure functions over what
jfrog.py read, so they test without a network."""

from __future__ import annotations

from .. import logic


def orphaned_mappings(mappings: list, service_accounts: dict) -> list:
    """service_accounts: {namespace: [sa names]} as far as the operator can read them.
    A mapping whose sub names a namespace/SA that does not exist is flagged; one in
    an unreadable namespace is 'unknown', not orphaned."""
    out = []
    for m in mappings or []:
        sub = (m.get("claims") or {}).get("sub", "")
        parts = sub.split(":")
        if len(parts) != 4 or parts[:2] != ["system", "serviceaccount"]:
            out.append({"mapping": m.get("name"), "sub": sub, "status": "fail", "detail": "sub is not system:serviceaccount:<ns>:<sa>"})
            continue
        ns, sa = parts[2], parts[3]
        if ns not in service_accounts:
            out.append({"mapping": m.get("name"), "sub": sub, "status": "unknown", "detail": f"namespace {ns} not readable or absent"})
        elif sa in service_accounts[ns]:
            out.append({"mapping": m.get("name"), "sub": sub, "status": "pass", "detail": f"{ns}/{sa} exists"})
        else:
            out.append({"mapping": m.get("name"), "sub": sub, "status": "fail",
                        "detail": f"{ns}/{sa} does not exist: an orphaned mapping (Prune is manual and defaults to false)"})
    return out


def binary_integrity(size, sha256=None, upstream_sha256=None) -> dict:
    problem = logic.binary_size_problem(size)
    if size is not None and size < logic.MIN_BINARY_BYTES:
        return {"status": "fail", "detail": problem}
    if sha256 and upstream_sha256:
        same = sha256 == upstream_sha256
        return {"status": "pass" if same and not problem else "fail",
                "detail": ("sha256 matches upstream" if same else "sha256 differs from upstream") + (f"; {problem}" if problem else "")}
    return {"status": "warn" if problem else "pass",
            "detail": (problem or f"{size:,} bytes") + "; sha256 vs upstream unverified (upstream not reachable from here)"}


def token_scope(groups_status: int) -> dict:
    if groups_status == 200:
        return {"status": "warn", "detail": "The token can read Access groups: broader than a download needs."}
    if groups_status == 403:
        return {"status": "info", "detail": "FORBIDDEN on /access/api/v2/groups: Artifactory rights without Access group "
                                             "management, as in the pilot. It is still the admin-scoped token: tighten it (open item)."}
    if groups_status == 401:
        return {"status": "fail", "detail": "401: the token is missing, expired or revoked."}
    return {"status": "unknown", "detail": f"HTTP {groups_status}"}


def realm(probe: dict, repo_key: str) -> dict:
    ok = logic.realm_names_repo(probe.get("www_authenticate", ""), repo_key)
    return {"status": "pass" if ok else "fail",
            "detail": (f"realm names {repo_key}: this host routes to the repo" if ok else
                       "realm does not name the repo key: the bare-domain path method does not work for local repos here; "
                       "use <server>-<repo-key>.jfrog.io")}


def remote(status: int, location: str) -> dict:
    if logic.is_dormant_redirect(status, location):
        return {"status": "fail", "detail": "Dormant repo, needs an admin (302 to reactivate-server)."}
    return {"status": "pass" if 200 <= status < 300 else "warn", "detail": f"HTTP {status}"}
