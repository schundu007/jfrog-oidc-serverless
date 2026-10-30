"""Pod events (CREATE_TEST_POD / Verify output) and kubelet's journal.

Pod events decide the pull verdict:
- `Successfully pulled image "..." in 10.487s ... Image size: 142944877 bytes` is pass evidence.
- `Container image "..." already present on machine` means no registry call:
  INCONCLUSIVE, never a pass.
- `failed to fetch anonymous token ... 401` is containerd saying no credential
  was supplied. It looks the same whether the plugin was never invoked or ran
  and failed.

kubelet's journal adds one line that matters: `error execing credential
provider plugin jfrog-credentials-provider ... exit status 1:` followed by
nothing. kubelet does not capture the plugin's output; the reason is in the
plugin's own log.
"""

from __future__ import annotations

import re

from .. import logic

ADO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s")

PULLED = re.compile(r'Successfully pulled image "(?P<image>[^"]+)" in (?P<secs>[\d.]+)s'
                    r'(?:.*?Image size: (?P<bytes>\d+) bytes)?')
PRESENT = re.compile(r'Container image "(?P<image>[^"]+)" already present on machine')
PULLING = re.compile(r'Pulling image "(?P<image>[^"]+)"')
ANON_401 = re.compile(r"failed to fetch anonymous token:? .*?(?P<url>https://\S+?)\s*:?\s*401")
UNSUPPORTED = re.compile(r"Unsupported [vV]2 repository request for '(?P<repo>[^']*)'")
FORBIDDEN = re.compile(r"(?P<line>.*\bForbidden\b.*)")
USER_PRINCIPAL = re.compile(r'(?:User|user)\s+"?(?P<who>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"?')
AZURE_FORBIDDEN = re.compile(r"does not have access to the resource in Azure")
PULL_403 = re.compile(r"(?:403 Forbidden|unexpected status.*\b403\b|denied: .*)", re.I)

JOURNAL_EXEC_FAILED = re.compile(r"Failed to provide credentials for image.*error execing credential provider plugin "
                                 r"(?P<plugin>[\w-]+).*exit status (?P<rc>\d+)")
JOURNAL_STARTED = re.compile(r"(?:Started|Starting) (?:kubelet|Kubernetes Kubelet)")


def _clean(text: str) -> list:
    return [ADO_PREFIX.sub("", l.rstrip("\r\n")) for l in (text or "").splitlines()]


def parse_pod_events(text: str) -> dict:
    out = {"pulling": [], "pulled": None, "already_present": None, "anon_401": None, "backoff": False,
           "err_image_pull": False, "created": False, "started": False, "running": False,
           "unsupported_repo": None, "forbidden": [], "pull_403": False, "lines": []}
    for line in _clean(text):
        if m := PULLED.search(line):
            out["pulled"] = {"image": m.group("image"), "seconds": float(m.group("secs")),
                             "bytes": int(m.group("bytes")) if m.group("bytes") else None, "line": line.strip()}
        elif m := PRESENT.search(line):
            out["already_present"] = {"image": m.group("image"), "line": line.strip()}
        elif m := PULLING.search(line):
            out["pulling"].append(m.group("image"))
        if m := ANON_401.search(line):
            out["anon_401"] = {"url": m.group("url"), "line": line.strip()}
        if "ImagePullBackOff" in line or "Back-off pulling image" in line:
            out["backoff"] = True
        if "ErrImagePull" in line:
            out["err_image_pull"] = True
        if re.search(r"\bCreated\b.*container|Created container", line):
            out["created"] = True
        if re.search(r"\bStarted\b.*container|Started container", line):
            out["started"] = True
        if re.search(r"\bRunning\b", line) and re.search(r"(?i)phase|status", line):
            out["running"] = True
        if m := UNSUPPORTED.search(line):
            out["unsupported_repo"] = {"repo": m.group("repo"), "line": line.strip()}
        if FORBIDDEN.search(line) and "anonymous token" not in line:
            who = USER_PRINCIPAL.search(line)
            out["forbidden"].append({"line": line.strip(), "user_principal": who.group("who") if who else None,
                                     "azure_rbac": bool(AZURE_FORBIDDEN.search(line))})
        if PULL_403.search(line) and "anonymous token" not in line and "Forbidden" not in line:
            out["pull_403"] = True
    return out


def pull_verdict(ev: dict) -> dict:
    """PASS / FAIL / INCONCLUSIVE for one pod run, with the evidence line."""
    if ev.get("pulled"):
        p = ev["pulled"]
        size = f", {p['bytes']:,} bytes" if p.get("bytes") else ""
        return {"status": "PASS", "evidence": p["line"],
                "detail": f"Pulled {p['image']} in {p['seconds']}s{size} with imagePullSecrets: []"}
    if ev.get("already_present"):
        return {"status": "INCONCLUSIVE", "evidence": ev["already_present"]["line"],
                "detail": "The image was already on the node: no registry call was made, so this run proves "
                          "nothing about authentication. Use a tag no node has pulled."}
    if ev.get("anon_401"):
        return {"status": "FAIL", "evidence": ev["anon_401"]["line"],
                "detail": "containerd got no credential: the plugin was not invoked, or was invoked and failed. "
                          "These look identical here; read the plugin's own log."}
    if ev.get("unsupported_repo") is not None:
        return {"status": "FAIL", "evidence": ev["unsupported_repo"]["line"],
                "detail": "Registry request against the bare domain (or a virtual repo): use the dedicated subdomain."}
    if ev.get("pull_403"):
        return {"status": "FAIL", "evidence": "403 on pull", "detail": "Token issued but the pull was refused."}
    if ev.get("backoff") or ev.get("err_image_pull"):
        return {"status": "FAIL", "evidence": "ErrImagePull / ImagePullBackOff", "detail": "The pull failed."}
    return {"status": "UNKNOWN", "evidence": "", "detail": "No pull events found in this output."}


def parse_journal(text: str) -> dict:
    out = {"exec_failed": [], "kubelet_starts": []}
    for line in _clean(text):
        if m := JOURNAL_EXEC_FAILED.search(line):
            out["exec_failed"].append({"plugin": m.group("plugin"), "rc": int(m.group("rc")), "line": line.strip()})
        if JOURNAL_STARTED.search(line):
            ts = re.match(r"^(\S+)", line)
            out["kubelet_starts"].append(logic.normalize_ts(ts.group(1)) if ts else "")
    return out
