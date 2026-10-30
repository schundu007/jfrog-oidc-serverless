"""Who writes the node's provider config (spec §6 `writers`).

Only the release's DaemonSet may mount the credential-provider config path.
A JFrog DaemonSet anywhere else (an orphan from an older install) is critical:
on sandbox one reverted the node file a second after every correct write.
"""

from __future__ import annotations

from .. import logic

WATCHED = ("/var/lib/kubelet", "credential-provider", "/etc/kubernetes")


def from_daemonsets(data: dict, cfg: dict) -> list:
    out = []
    for d in data.get("items", []):
        md, spec = d.get("metadata") or {}, ((d.get("spec") or {}).get("template") or {}).get("spec") or {}
        paths = [(v.get("hostPath") or {}).get("path") for v in spec.get("volumes") or [] if v.get("hostPath")]
        mounts = [p for p in paths if p and any(w in p for w in WATCHED)]
        images = [c.get("image", "") for c in (spec.get("containers") or []) + (spec.get("initContainers") or [])]
        jfrog = "jfrog" in (md.get("name", "") + " ".join(images)).lower()
        if not mounts and not jfrog:
            continue
        out.append({"namespace": md.get("namespace"), "name": md.get("name"), "mounts": mounts, "jfrog": jfrog,
                    "release": md.get("namespace") == cfg["k8s"]["namespace"] and md.get("name") == cfg["k8s"]["daemonset"],
                    "created": md.get("creationTimestamp"), "images": images,
                    "ready": (d.get("status") or {}).get("numberReady", 0), "desired": (d.get("status") or {}).get("desiredNumberScheduled", 0)})
    return out


def check(daemonsets: list) -> list:
    orphans = [d for d in daemonsets if d["jfrog"] and not d["release"]]
    others = [d for d in daemonsets if not d["jfrog"] and not d["release"]]
    res = []
    if orphans:
        res.append({"id": "writers", "title": "Writers of the node provider config", "status": "fail",
                    "detail": "Orphaned JFrog DaemonSet(s): " + ", ".join(f"{d['namespace']}/{d['name']} (created {d['created']})" for d in orphans)
                              + ". Critical: it will revert the node file after every correct write.",
                    "fix": "CLEANUP_STALE_JFROG_DAEMONSET (DryRun true first). If the config still reverts afterwards, replace the node.",
                    "fallback": None})
    else:
        res.append({"id": "writers", "title": "Writers of the node provider config", "status": "pass",
                    "detail": "Only the release's DaemonSet is a JFrog writer" + (
                        f"; other DaemonSets mount kubelet paths: {', '.join(d['namespace'] + '/' + d['name'] for d in others)}" if others else ""),
                    "fix": None, "fallback": None})
    return res
