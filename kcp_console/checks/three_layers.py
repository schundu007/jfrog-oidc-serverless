"""The headline check: values file vs live Helm values vs rendered manifest vs
the node file's JFrog entry vs the plugin's newest token URL, per node.

Three layers each looked correct on sandbox while the result was wrong. Any
disagreement turns the node's row red and names the fix for that layer.
"""

from __future__ import annotations

from .. import logic
from ..parse import helm, summary

FIELDS = ("artifactory_url", "match_images", "provider_name", "default_cache_duration")

FIX = {
    "values": "Fix scripts/jfrog-provider/values-sandbox.yaml (the console renders it) and re-run INSTALL_JFROG_PROVIDER.",
    "live": "The live release differs from the committed file: re-run INSTALL_JFROG_PROVIDER with the committed values.",
    "manifest": "The rendered manifest differs from the values: re-run INSTALL_JFROG_PROVIDER and check the chart version.",
    "node": ("The node file kept an old entry (MergeFiles replaces only an identical artifactory_url): run "
             "RESET_NODE_JFROG_ENTRY with DryRun true, then false, then CHECK_KUBELET_PROVIDER."),
    "plugin": ("The node file is right but the plugin still builds a doubled URL: kubelet is running the config it read "
               "at its own start. Compare Kubelet started at with Config file mtime."),
}


def _norm(field, value):
    if field == "match_images":
        return sorted(str(x).lower() for x in value or [])
    return None if value in (None, "") else str(value)


def compare(values_text: str, helm_values_text: str = "", manifest_text: str = "", kubelet_check_text: str = "") -> dict:
    layers = {"values": helm.values_summary(values_text or "")}
    if helm_values_text:
        layers["live"] = helm.values_summary(helm_values_text)
    if manifest_text:
        layers["manifest"] = helm.manifest_summary(manifest_text)
    qs = summary.quick_summary(kubelet_check_text) if kubelet_check_text else {"nodes": [], "source": None}
    want = layers["values"]
    nodes = []
    for n in qs["nodes"] or [None]:
        row_layers = dict(layers)
        problems = []
        if n is not None:
            row_layers["node"] = n["entry"]
        for name, layer in row_layers.items():
            if name == "values" or not layer:
                continue
            for f in FIELDS:
                if layer.get(f) not in (None, [], "") and _norm(f, layer.get(f)) != _norm(f, want.get(f)):
                    problems.append({"layer": name, "field": f, "have": layer.get(f), "want": want.get(f), "fix": FIX[name]})
        if err := logic.bare_host_error(want.get("artifactory_url") or ""):
            problems.append({"layer": "values", "field": "artifactory_url", "have": want.get("artifactory_url"),
                             "want": "a bare hostname", "fix": FIX["values"] + f" artifactoryUrl {err}."})
        plugin = (n or {}).get("plugin") or {}
        if n is not None:
            if not n["entry"]:
                problems.append({"layer": "node", "field": "entry", "have": "no JFrog entry", "want": "one JFrog entry",
                                 "fix": "No JFrog entry on this node. If this is not the pod's node, check the right node; "
                                        "otherwise check that the DaemonSet runs here (node label, taints)."})
            elif n["entry"].get("entries", 1) != 1:
                problems.append({"layer": "node", "field": "entry", "have": f"{n['entry']['entries']} JFrog entries",
                                 "want": "one", "fix": FIX["node"]})
            if plugin.get("newest_schemes") and plugin["newest_schemes"] != 1:
                node_ok = logic.is_bare_host(n["entry"].get("artifactory_url") or "")
                problems.append({"layer": "plugin", "field": "token url", "have": plugin["newest_token_url"],
                                 "want": "exactly one https://", "fix": FIX["plugin"] if node_ok else FIX["node"]})
            if n.get("kubelet_older_than_config"):
                problems.append({"layer": "kubelet", "field": "kubelet start", "have": n["kubelet_started"],
                                 "want": f"after {n['config_mtime']}",
                                 "fix": "kubelet is running an older config: it started before the file was last written."})
        nodes.append({
            "node": (n or {}).get("node"), "reason": (n or {}).get("reason"),
            "layers": {k: {f: v.get(f) for f in FIELDS} for k, v in row_layers.items() if v},
            "plugin": {"newest_token_url": plugin.get("newest_token_url"), "schemes": plugin.get("newest_schemes"),
                       "time": plugin.get("newest_token_url_time")},
            "kubelet_started": (n or {}).get("kubelet_started"), "config_mtime": (n or {}).get("config_mtime"),
            "kubelet_older_than_config": (n or {}).get("kubelet_older_than_config"),
            "status": "fail" if problems else "pass", "problems": problems,
        })
    return {"status": "fail" if any(r["status"] == "fail" for r in nodes) else "pass", "nodes": nodes,
            "source": qs.get("source")}
