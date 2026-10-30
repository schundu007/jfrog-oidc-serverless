"""The troubleshooting rule engine (runbook §9, plus the rules the bug table adds).

Input is an evidence bundle: what runs collected, in pipeline-output form
(pod events, CHECK_KUBELET_PROVIDER output with its QUICK SUMMARY, the plugin
log, node files, Helm values and manifest, install/reset logs, Artifactory
checks). The simulator builds the same bundle, so scenarios exercise this file.

Output: a verdict for the newest test pod and a ranked list of findings, each
with the evidence lines that matched and the next check to run (dry run first).
"""

from __future__ import annotations

import re

from . import knowledge, logic
from .checks import three_layers
from .parse import events, plugin_log, summary

TROUBLE = {t["id"]: t for t in knowledge.TROUBLESHOOTING}


def _f(rule, score, title, evidence, next_scenario=None, dry_run=True, level="cause", **extra):
    t = TROUBLE.get(rule, {})
    return {"rule": rule, "score": score, "title": title, "level": level,
            "evidence": [e for e in evidence if e][:6],
            "where": t.get("where", ""), "observed": t.get("observed", True),
            "label": "" if t.get("observed", True) else knowledge.NOT_OBSERVED_LABEL,
            "next": {"scenario": next_scenario, "dry_run": dry_run} if next_scenario else None, **extra}


def _latest_check(bundle):
    checks = bundle.get("kubelet_checks") or []
    return checks[-1] if checks else ""


def pod_verdict(pod: dict, kubelet_check: str = "", cache_seconds=None) -> dict:
    ev = events.parse_pod_events(pod.get("events", ""))
    v = events.pull_verdict(ev)
    v.update({"pod": pod.get("name"), "image": pod.get("image"), "sa": pod.get("sa"), "node": pod.get("node")})
    if v["status"] == "PASS":
        qs = summary.quick_summary(kubelet_check) if kubelet_check else {"nodes": []}
        node = next((n for n in qs["nodes"] if logic.same_node(n.get("node"), pod.get("node"))), None)
        newest = (node or {}).get("plugin", {}).get("newest_token_url_time")
        if node is None:
            v["exchange"] = "unknown: run CHECK_KUBELET_PROVIDER on the pod's node to read the plugin log"
        elif newest and pod.get("time") and newest >= _minus(pod["time"], 120):
            v["exchange"] = "fresh: a new RT oidc token url line was logged for this pull"
        else:
            v["exchange"] = ("cached: no new RT oidc token url line, so kubelet reused a cached credential. This proves "
                             "the pull works, not that a fresh exchange happened")
    return v


def _minus(ts, seconds):
    import datetime as dt
    try:
        t = dt.datetime.strptime(logic.normalize_ts(ts), "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(seconds=seconds)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ts


def diagnose(bundle: dict) -> dict:
    cfg = bundle.get("config") or {}
    want = (cfg.get("values") or {})
    pods = bundle.get("pods") or []
    tests = [p for p in pods if p.get("kind", "test") == "test"]
    controls = [p for p in pods if p.get("kind") == "control"]
    check = _latest_check(bundle)
    qs = summary.quick_summary(check) if check else {"nodes": [], "source": None, "chars": 0}
    node = qs["nodes"][-1] if qs["nodes"] else None
    layers = three_layers.compare(bundle.get("values_file", ""), bundle.get("helm_values", ""),
                                  bundle.get("manifest", ""), check) if bundle.get("values_file") else None
    findings = []
    last = tests[-1] if tests else None
    verdict = pod_verdict(last, check) if last else None
    pev = events.parse_pod_events(last["events"]) if last else {}
    failed = bool(verdict and verdict["status"] == "FAIL")
    plugin = (node or {}).get("plugin") or {}
    entry = (node or {}).get("entry") or {}
    pod_node = (last or {}).get("node")

    # --- the wrong node was inspected (node 0 instead of the pod's node)
    if node and pod_node and not logic.same_node(node["node"], pod_node):
        findings.append(_f("wrong-node-checked", 92,
                           f"CHECK_KUBELET_PROVIDER inspected {node['node']}, but the pod ran on {pod_node}",
                           [f"Node inspected: {node['node']}", f"Node chosen because: {node.get('reason')}"],
                           "CHECK_KUBELET_PROVIDER", False))
    elif node and re.search(r"node 0|first node", node.get("reason") or "", re.I) and not entry:
        findings.append(_f("wrong-node-checked", 80, "The check fell back to the first node and found no JFrog entry",
                           [f"Node chosen because: {node.get('reason')}"], "CHECK_KUBELET_PROVIDER", False))

    # --- reverting config (orphaned writer)
    revert = _reverts(bundle.get("kubelet_checks") or [], want)
    orphans = bundle.get("daemonsets") or []
    for d in orphans:
        if entry and sorted(entry.get("match_images") or []) == sorted(d.get("match_images") or []) \
                and entry.get("provider_name") == d.get("provider_name"):
            revert.append(f"{node['node']}: the node entry (matchImages={entry.get('match_images')}, provider="
                          f"{entry.get('provider_name')}) is {d['namespace']}/{d['name']}'s, not this release's")
    if revert or orphans:
        ev = [f"orphaned DaemonSet {d['namespace']}/{d['name']} ({d.get('age_days', '?')} days old) mounts "
              f"{', '.join(d.get('mounts') or [])}{' (deleted)' if d.get('deleted') else ''}" for d in orphans] + revert
        findings.append(_f("config-reverts", 96 if revert else 70,
                           "Another writer is reverting the node's provider config" if revert
                           else "An orphaned JFrog DaemonSet from an older install writes the same host path (critical)",
                           ev, "CLEANUP_STALE_JFROG_DAEMONSET" if any(not d.get("deleted") for d in orphans)
                           else "FIND_CREDENTIAL_PROVIDER_WRITERS", True,
                           hint="If it keeps reverting after the DaemonSet is gone, replace the node (that is what "
                                "cleared it on sandbox). Ruled out on sandbox: nodepool config, Azure Policy, VMSS extensions"))

    # --- kubelet running an older config
    if node and node.get("kubelet_older_than_config"):
        findings.append(_f("kubelet-old-config", 95 if logic.is_bare_host(entry.get("artifactory_url") or "") else 60,
                           "kubelet is running an older config: it started before the provider config was last written",
                           [f"Kubelet started at: {node['kubelet_started']}", f"Config file mtime: {node['config_mtime']}"],
                           "CHECK_KUBELET_PROVIDER", False))

    # --- doubled https://
    if plugin.get("newest_schemes") and plugin["newest_schemes"] > 1:
        values_bare = logic.is_bare_host(want.get("artifactory_url") or "")
        stale = values_bare and not logic.is_bare_host(entry.get("artifactory_url") or "")
        if stale:
            title = ("Stale node entry: the values and release are bare, but the node file kept artifactory_url="
                     f"{entry.get('artifactory_url')}, so the plugin builds https://https://")
        elif values_bare and logic.is_bare_host(entry.get("artifactory_url") or "x://"):
            title = ("The node file is bare, but the plugin still builds https://https://: kubelet is running the "
                     "config it read at its own start")
        else:
            title = "artifactory_url has a scheme: the plugin builds https://https:// and DNS fails before any exchange"
        findings.append(_f("doubled-https", 90 if not (node and node.get("kubelet_older_than_config") and values_bare
                                                        and not stale) else 70, title,
                           [plugin.get("newest_token_url") and f"RT oidc token url :{plugin['newest_token_url']}",
                            entry.get("artifactory_url") and f"node entry artifactory_url: {entry['artifactory_url']}",
                            f"values artifactoryUrl: {want.get('artifactory_url')}"],
                           "RESET_NODE_JFROG_ENTRY" if stale else None, True, stale=stale))
    elif failed and not values_ok(want):
        findings.append(_f("doubled-https", 85, "artifactoryUrl in the values has a scheme; the plugin will double it",
                           [f"values artifactoryUrl: {want.get('artifactory_url')}"]))

    # --- matchImages
    if failed and last and node and entry and not any(logic.match_image(m, last["image"]) for m in entry.get("match_images") or []):
        findings.append(_f("matchimages-mismatch", 93,
                           f"matchImages on {node['node']} ({', '.join(entry.get('match_images') or [])}) does not match "
                           f"{logic.image_host(last['image'])}: kubelet never invoked the plugin",
                           [f"pod image: {last['image']}", f"node matchImages: {entry.get('match_images')}"],
                           "INSTALL_JFROG_PROVIDER", True))

    # --- wrong provider name (another cluster's)
    if node and entry.get("provider_name") and want.get("provider_name") and entry["provider_name"] != want["provider_name"]:
        protected = entry["provider_name"] in ((cfg.get("jfrog") or {}).get("protected_providers") or [])
        findings.append(_f("wrong-provider-name", 91 if failed else 50,
                           f"The node uses OIDC provider {entry['provider_name']}"
                           f"{' (another cluster’s, protected)' if protected else ''}; this cluster's is {want['provider_name']}",
                           [f"jfrog_oidc_provider_name: {entry['provider_name']}"], "INSTALL_JFROG_PROVIDER", True))

    # --- the token endpoint refused (expected, not observed)
    plines = [l for l in check.splitlines() if "oidc token request returned status" in l]
    if plines or plugin.get("token_http_errors"):
        findings.append(_f("token-endpoint-refused", 88,
                           "Artifactory refused the exchange: provider, issuer or identity-mapping mismatch (sub must equal "
                           f"system:serviceaccount:{(cfg.get('k8s') or {}).get('namespace')}:{(cfg.get('k8s') or {}).get('service_account')})",
                           plines[-2:], None))

    # --- token issued, pull 403 (expected, not observed)
    if pev.get("pull_403") or (last and "403 Forbidden" in last.get("events", "")):
        findings.append(_f("pull-403", 89, "The token was issued but the pull was refused: the mapped user lacks permission on the repo",
                           [l.strip() for l in last["events"].splitlines() if "403" in l][:2]))

    # --- bare domain / virtual promote
    if pev.get("unsupported_repo") is not None:
        findings.append(_f("unsupported-v2", 92, "Registry request against the bare domain: use the repo's dedicated subdomain",
                           [pev["unsupported_repo"]["line"]]))
    for pr in bundle.get("promotes") or []:
        if pr.get("status") != 200 and "Unsupported V2 repository request" in pr.get("body", ""):
            findings.append(_f("promote-virtual", 92, f"Promote from {pr['source']} failed: the source must be a local repo "
                                                      f"(find it with AQL)", [pr["body"]]))
    for p in bundle.get("probes") or []:
        if not logic.realm_names_repo(p.get("www_authenticate", ""), _repo_for_host(cfg, p["host"])):
            findings.append(_f("unsupported-v2", 80, f"The realm from {p['host']} does not name the repo key: the bare-domain "
                                                     f"path method does not work for local repos here", [p.get("www_authenticate")]))

    # --- dormant remote
    rem = bundle.get("remote")
    if rem and logic.is_dormant_redirect(rem.get("status", 0), rem.get("location", "")):
        findings.append(_f("dormant-remote", 90, "Dormant repo, needs an admin: JFrog Cloud paused it (302 to reactivate-server)",
                           [f"302 → {rem['location']}"]))

    # --- install-time failures
    for log in bundle.get("install_logs") or []:
        if "curl: (22)" in log and "401" in log and any(want.get("download_url", "").endswith(s) or s + s in log
                                                         for s in logic.ARCH_SUFFIXES):
            findings.append(_f("downloadurl-arch", 95, "downloadUrl ends in an arch suffix; the chart appends its own, so the "
                                                       "init container's download 401s", [l for l in log.splitlines() if "curl" in l]))
        if "timed out waiting for the condition" in log:
            findings.append(_f("helm-wait", 97, "helm --wait timed out: a ConfigMap change does not restart the provider pods. "
                                                "Drop --wait and delete the pods after install", [l for l in log.splitlines() if "timed out" in l],
                               "INSTALL_JFROG_PROVIDER", True))
    m = bundle.get("mirror")
    sizes = [m["size"]] if m else []
    if node and node.get("binary_bytes") is not None:
        sizes.append(node["binary_bytes"])
    if any(s is not None and s < logic.MIN_BINARY_BYTES for s in sizes):
        findings.append(_f("zero-byte-binary", 96, "The plugin binary is under 1 MB: curl saved a redirect body (use -L and the size guard)",
                           [f"binary size: {min(s for s in sizes if s is not None)} bytes"], "MIRROR_JFROG_BINARY", False))

    # --- reset safety (old behaviour)
    for log in bundle.get("reset_logs") or []:
        aborts = [l for l in log.splitlines() if "ABORT" in l and "name: artifactory_url" in l]
        if aborts:
            findings.append(_f("reset-safety-old", 90, "RESET_NODE_JFROG_ENTRY aborted on the JFrog entry's own env: items: "
                                                       "the safety check must count only provider-level entries", aborts))

    # --- generic anonymous 401
    if pev.get("anon_401"):
        findings.append(_f("anon-401", 40, "containerd got no credential: the plugin was not invoked, or was invoked and failed. "
                                           "These look identical in the pod events", [pev["anon_401"]["line"][:240]],
                           "CHECK_KUBELET_PROVIDER", False))
    journal = events.parse_journal(check)
    if journal["exec_failed"] and failed:
        findings.append(_f("plugin-exec-failed", 35, "kubelet invoked the plugin and it exited 1. kubelet does not capture the "
                                                     "plugin's output; read the plugin's own log", [journal["exec_failed"][-1]["line"][:240]],
                           level="info"))

    # --- Forbidden lines (RBAC, user principal)
    for p in pods:
        for fb in events.parse_pod_events(p.get("events", ""))["forbidden"]:
            if fb["azure_rbac"]:
                findings.append(_f("azure-forbidden", 55, "Personal accounts are read-only: run this as a pipeline scenario", [fb["line"]]))
            if fb["user_principal"]:
                findings.append(_f("forbidden-user-principal", 75, f"Forbidden attributed to a user principal "
                                   f"({fb['user_principal']}) rather than the service principal: the stage is not authenticating "
                                   f"the way we assume", [fb["line"]]))

    # --- read-only diagnostic artifact
    if plugin.get("readonly_fs") or "read-only file system" in check:
        findings.append(_f("diagnostic-readonly", 20, "Diagnostic artifact, not a broken binary: the diagnostic pod mounts the host "
                                                      "read-only. Read the plugin's log file instead", [], level="info"))

    # --- truncated output
    if qs.get("source") == "full-log" and check:
        findings.append(_f("output-truncated", 45, "No QUICK SUMMARY footer: the output was cut off (the ~50,000-character limit). "
                                                   "Parsed the full log instead", [f"{qs.get('chars', 0):,} characters"], level="info"))

    # --- inconclusive and cached
    if verdict and verdict["status"] == "INCONCLUSIVE":
        findings.append(_f("cached-image", 30, "The image was already on the node: this run proves nothing about authentication. "
                                               "Promote a fresh tag and pass it as TEST_POD_IMAGE", [verdict["evidence"]],
                           "CREATE_TEST_POD", False, level="info"))
    if verdict and verdict["status"] == "PASS" and verdict.get("exchange", "").startswith("cached"):
        findings.append(_f("cached-token-silent", 25, "The pull worked inside the cache window with no new token URL line: it "
                                                      "proves the pull works, not that a fresh exchange happened", [], level="info"))

    # --- control pod
    control = None
    for c in controls:
        cv = events.pull_verdict(events.parse_pod_events(c["events"]))
        control = {"pod": c["name"], "sa": c["sa"], "refused": cv["status"] == "FAIL", "evidence": cv["evidence"]}
        findings.append(_f("control-refused" if control["refused"] else "control-pulled", 15 if control["refused"] else 97,
                           f"Control pod with service account {c['sa']} was "
                           + ("refused, as intended: access is tied to the identity, not the node" if control["refused"]
                              else "able to pull: access is NOT tied to the identity"),
                           [cv["evidence"]], level="info" if control["refused"] else "cause"))

    # --- audit attribution
    audit = attribution(bundle.get("audit") or [], (cfg.get("jfrog") or {}).get("mapped_user"))

    findings.sort(key=lambda f: (-f["score"], f["rule"]))
    causes = [f for f in findings if f["level"] == "cause"]
    status = verdict["status"] if verdict else ("FAIL" if causes else "UNKNOWN")
    if control and not control["refused"]:
        status = "FAIL"
    root = causes[0]["rule"] if causes and status in ("FAIL", "UNKNOWN") else (findings[0]["rule"] if findings and status != "PASS" else None)
    if status == "PASS":
        root = "cached-token-silent" if any(f["rule"] == "cached-token-silent" for f in findings) else None
    elif status == "INCONCLUSIVE":
        root = "cached-image"
    # The proof is the newest pod that actually pulled, which may be older than the newest run.
    proof = None
    for p in reversed(tests):
        if events.pull_verdict(events.parse_pod_events(p["events"]))["status"] == "PASS":
            proof = pod_verdict(p, check)
            break
    return {"verdict": {"status": status, "root_cause": root, "pod": verdict, "proof": proof, "control": control, "audit": audit},
            "findings": findings, "three_layers": layers}


def values_ok(v):
    return logic.is_bare_host(v.get("artifactory_url") or "")


def _repo_for_host(cfg, host):
    j = cfg.get("jfrog") or {}
    server, _, domain = (j.get("host") or "").partition(".")
    if host.startswith(server + "-") and host.endswith("." + domain):
        return host[len(server) + 1: -(len(domain) + 1)]
    return j.get("test_repo", "")


def _reverts(checks: list, want: dict) -> list:
    """Two consecutive checks where the node file goes back from the release's values to an older one,
    or a single check whose config mtime is within seconds of a kubelet restart that followed a correct write."""
    out, prev = [], {}
    for text in checks:
        for n in summary.quick_summary(text)["nodes"]:
            e = n["entry"]
            key = (n["node"] or "").lower()
            good = (e.get("match_images") and sorted(e["match_images"]) == sorted(want.get("match_images") or [])
                    and e.get("provider_name") == want.get("provider_name"))
            if prev.get(key) is True and not good and e:
                out.append(f"{n['node']}: config went back to matchImages={e.get('match_images')}, "
                           f"provider={e.get('provider_name')} after a correct write")
            prev[key] = bool(good)
    for text in checks[-1:]:
        for line in text.splitlines():
            if "reverted the node file" in line:
                out.append(line.strip())
    return out


def attribution(audit: list, mapped_user: str) -> dict:
    """The pull's audit records: who Artifactory attributed the manifest GET to."""
    pulls = [a for a in audit if a.get("service") == "artifactory" and "/manifests/" in a.get("path", "") and a.get("status") == 200]
    if not pulls:
        return {"status": "NOT RUN", "detail": "No pull records in the audit evidence yet.", "steps": knowledge.AUDIT["manual_steps"]}
    last = pulls[-1]
    ok = last.get("user") == mapped_user
    return {"status": "PASS" if ok else "FAIL", "user": last.get("user"), "time": last.get("t"), "path": last.get("path"),
            "detail": (f"Pulled as {last.get('user')}, the identity the mapping resolves to." if ok else
                       f"Pulled as {last.get('user')}, not the mapped {mapped_user}: a static account may still be in use.")}
