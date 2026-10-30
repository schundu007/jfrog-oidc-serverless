"""Builds kcp_console/tests/fixtures/sandbox/: the replay fixtures and seeded history for
`python3 -m kcp_console --fixtures kcp_console/tests/fixtures/sandbox`.

The pipeline logs come from the simulator driven through the sandbox story (the
doubled URL, the stale node entry, the reset, the pull of 142,944,877 bytes in
10.487 s), in the formats the real pipeline prints. Deterministic: rerun after
changing the simulator, and test_fixtures checks the files are current.

    python3 -m kcp_console.tests.fixtures.make_fixtures
"""

from __future__ import annotations

import copy
import json
import pathlib

from kcp_console import knowledge, logic, model
from kcp_console.ado import ADO_RESOURCE, param_args
from kcp_console.runner import build_request
from kcp_console.sim.world import World, iso
from kcp_console.tests import runbook_strings as rs

OUT = pathlib.Path(__file__).with_name("sandbox")
ORG, PROJECT = "example-org", "ArgoCD"
ORG_URL = f"https://dev.azure.com/{ORG}"
NODE_E, NODE_G = "aks-nodepool1-12345678-vmss00000e", "aks-nodepool1-12345678-vmss00000g"


def config() -> dict:
    cfg = model.default_config("sandbox-pilot")
    cfg["ado"].update({"organization": ORG, "project": PROJECT})
    cfg["template"].update({"ClusterName": cfg["cluster"]["name"], "SubscriptionId": cfg["cluster"]["subscription_id"],
                            "MatchImages": cfg["jfrog"]["test_repo_host"], "BinaryMirrorRepo": cfg["jfrog"]["binary_repo"],
                            "OidcProviderName": cfg["values"]["provider_name"], "TestImage": cfg["jfrog"]["test_image"]})
    return cfg


def ado_log(w: World, scenario: str, body: str, dry=None) -> str:
    head = [f"Starting: {scenario}", f"TestScenario={scenario}" + (f" DryRun={str(dry).lower()}" if dry is not None else "")]
    stamp = iso(w.t)[:-1] + ".0000000Z "
    return "\n".join(stamp + l for l in head + body.splitlines() + [f"Finishing: {scenario}"])


def pod_log(w: World, pod: dict) -> str:
    return pod["events"]


def story():
    """(scenario, kind, params, log text, ado result) in pilot order."""
    cfg = config()
    w = World(cfg)
    out = []
    https = {"artifactory_url": "https://example.jfrog.io"}
    w.mirror_binary()
    out.append(("MIRROR_JFROG_BINARY", "pilot", {}, ado_log(w, "MIRROR_JFROG_BINARY", w.evidence["mirror"]["log"]), "succeeded"))
    w.create_provider(); w.create_mapping(); w.foreign_provider()
    out.append(("create-provider", "jfrog", {}, "", 201))
    out.append(("create-mapping", "jfrog", {}, "", 201))
    w.promote(cfg["jfrog"]["promote_source_repo"], cfg["jfrog"]["test_repo"], "mcp/sonarqube", "1.26-v1")
    out.append(("promote", "jfrog", {"source": cfg["jfrog"]["promote_source_repo"]}, "Promotion ended successfully", 200))
    w.install(values=https)
    out.append(("INSTALL_JFROG_PROVIDER", "pilot", {"DryRun": False}, ado_log(w, "INSTALL_JFROG_PROVIDER", w.install_logs[-1], False), "succeeded"))
    w.advance(600)
    p = w.create_pod(node=NODE_G)
    out.append(("CREATE_TEST_POD", "pilot", {}, ado_log(w, "CREATE_TEST_POD", p["events"]), "succeeded"))
    out.append(("CHECK_KUBELET_PROVIDER", "pilot", {}, ado_log(w, "CHECK_KUBELET_PROVIDER", w.check_kubelet_provider()), "succeeded"))
    w.advance(900)
    w.install()
    out.append(("INSTALL_JFROG_PROVIDER", "pilot", {"DryRun": False}, ado_log(w, "INSTALL_JFROG_PROVIDER", w.install_logs[-1], False), "succeeded"))
    p = w.create_pod(node=NODE_G)
    out.append(("CREATE_TEST_POD", "pilot", {}, ado_log(w, "CREATE_TEST_POD", p["events"]), "succeeded"))
    out.append(("CHECK_JFROG_POD_CONFIG", "pilot", {}, ado_log(w, "CHECK_JFROG_POD_CONFIG", w.check_pod_config()), "succeeded"))
    out.append(("CHECK_KUBELET_PROVIDER", "pilot", {}, ado_log(w, "CHECK_KUBELET_PROVIDER", w.check_kubelet_provider()), "succeeded"))
    w.advance(300)
    w.reset_node_entries(dry_run=True)
    out.append(("RESET_NODE_JFROG_ENTRY", "pilot", {"DryRun": True}, ado_log(w, "RESET_NODE_JFROG_ENTRY", w.reset_logs[-1], True), "succeeded"))
    w.reset_node_entries(dry_run=False)
    out.append(("RESET_NODE_JFROG_ENTRY", "pilot", {"DryRun": False}, ado_log(w, "RESET_NODE_JFROG_ENTRY", w.reset_logs[-1], False), "succeeded"))
    w.advance(60)
    p = w.create_pod(node=NODE_G)
    out.append(("CREATE_TEST_POD", "pilot", {}, ado_log(w, "CREATE_TEST_POD", p["events"]), "succeeded"))
    out.append(("CHECK_KUBELET_PROVIDER", "pilot", {}, ado_log(w, "CHECK_KUBELET_PROVIDER", w.check_kubelet_provider()), "succeeded"))
    w.advance(3600)
    p = w.create_pod(node=NODE_G)
    out.append(("CREATE_TEST_POD", "pilot", {}, ado_log(w, "CREATE_TEST_POD", p["events"]), "succeeded"))
    return cfg, w, out


def history(cfg, w, steps) -> list:
    """Seeded run records, as the runner would have stored them."""
    runs = []
    t = 0
    for i, (name, kind, params, log, result) in enumerate(steps):
        c = copy.deepcopy(cfg)
        if name == "INSTALL_JFROG_PROVIDER" and i < 6:
            c["values"]["artifactory_url"] = "https://example.jfrog.io"
        r = build_request(c, kind, name, params)
        t += 1
        rid = f"20261016-12{t:02d}00-{i:06x}"
        when = f"2026-10-16T12:{t:02d}:00Z"
        rec = {"id": rid, "created": when, "finished": when, "config": "sandbox-pilot", "seeded": True, **r,
               "status": "succeeded" if result in ("succeeded", 200, 201) else "failed",
               "approved": {"at": when, "changes_state": r["changes_state"]},
               "events": [{"t": when, "kind": "seeded", "text": "recorded during the sandbox pilot (replay fixture)"}]}
        rec["guards"] = []
        if kind == "jfrog":
            rec["result"] = {"status": result, "body": log}
            log = ""
        else:
            rec["ado_run_id"] = 4100 + i
            rec["result"] = {"ado_result": result, "chars": len(log)}
        runs.append({"run": rec, "log": log})
    return runs


def shell_fixtures(cfg, w, steps) -> list:
    fx = []
    scope = ["--org", ORG_URL, "--project", PROJECT, "-o", "json"]
    c = cfg["cluster"]
    after = w  # final world state
    # -- Discover reads
    fx.append({"argv": ["az", "aks", "show", "--resource-group", c["resource_group"], "--name", c["name"],
                        "--subscription", c["subscription_id"], "-o", "json"], "rc": 0, "stdout": json.dumps({
        "name": c["name"], "kubernetesVersion": "1.31.7", "oidcIssuerProfile": {"enabled": True, "issuerUrl": c["oidc_issuer"]},
        "securityProfile": {"workloadIdentity": {"enabled": True}}})})
    sub = f"/subscriptions/{c['subscription_id']}/resourcegroups/{c['resource_group']}/providers/Microsoft.ContainerService/managedClusters/"
    fx.append({"argv": ["az", "aks", "list", "-o", "json"], "rc": 0, "stdout": json.dumps([
        {"id": sub + c["name"], "name": c["name"], "resourceGroup": c["resource_group"], "location": "centralus",
         "kubernetesVersion": "1.31.7", "powerState": {"code": "Running"},
         "oidcIssuerProfile": {"enabled": True, "issuerUrl": c["oidc_issuer"]}, "securityProfile": {"workloadIdentity": {"enabled": True}}},
        {"id": sub + "aks-sandbox-02", "name": "aks-sandbox-02", "resourceGroup": c["resource_group"], "location": "centralus",
         "kubernetesVersion": "1.30.4", "powerState": {"code": "Running"}, "oidcIssuerProfile": {"enabled": False}}])})
    ds = {"items": [
        {"metadata": {"name": cfg["k8s"]["daemonset"], "namespace": "jfrog", "creationTimestamp": "2026-10-16T12:05:00Z"},
         "spec": {"template": {"spec": {"volumes": [{"name": "host", "hostPath": {"path": "/var/lib/kubelet"}}],
                                        "initContainers": [{"image": "releases-docker.jfrog.io/jfrog/jfrog-credential-provider:1.4.0"}],
                                        "containers": [{"image": "mcr.microsoft.com/azurelinux/base/core:3.0"}]}}}},
        {"metadata": {"name": "azure-ip-masq-agent", "namespace": "kube-system", "creationTimestamp": "2026-07-29T10:00:00Z"},
         "spec": {"template": {"spec": {"volumes": [{"name": "cfg", "configMap": {"name": "x"}}], "containers": [{"image": "mcr.microsoft.com/oss/ip-masq-agent:v2"}]}}}}]}
    fx.append({"argv": ["kubectl", "get", "daemonsets", "-A", "-o", "json"], "rc": 0, "stdout": json.dumps(ds)})
    fx.append({"argv": ["kubectl", "get", "serviceaccount", "jfrog-provider-sa", "-n", "jfrog", "-o", "json"], "rc": 0,
               "stdout": json.dumps({"metadata": {"name": "jfrog-provider-sa", "namespace": "jfrog",
                                                  "creationTimestamp": "2026-07-29T10:00:00Z", "annotations": {"JFrogExchange": "true"}}})})
    fx.append({"argv": ["kubectl", "get", "nodes", "-o", "json"], "rc": 1, "stdout": "",
               "stderr": 'Error from server (Forbidden): nodes is forbidden: User "operator@example.com" cannot list resource "nodes" '
                         'in API group "" at the cluster scope: User does not have access to the resource in Azure. Update role assignment to allow access.'})
    fx.append({"argv": ["kubectl", "get", "pods", "-n", "jfrog", "-o", "json"], "rc": 0, "stdout": json.dumps({"items": [
        {"metadata": {"name": "secretless-pull-test"}, "spec": {"serviceAccountName": "jfrog-provider-sa", "nodeName": NODE_G}},
        {"metadata": {"name": "pilot-test-pod"}, "spec": {"serviceAccountName": "default"}}]})})
    fx.append({"argv": ["helm", "list", "-n", "jfrog", "-o", "json"], "rc": 0, "stdout": json.dumps([
        {"name": "jfrog-cred-provider", "namespace": "jfrog", "revision": "15", "status": "deployed",
         "chart": "jfrog-credential-provider-1.4.0", "app_version": "1.4.0"}])})
    fx.append({"argv": ["helm", "get", "values", "jfrog-cred-provider", "-n", "jfrog", "-a", "-o", "yaml"], "rc": 0,
               "stdout": model.render_values(cfg)})
    fx.append({"argv": ["helm", "get", "manifest", "jfrog-cred-provider", "-n", "jfrog"], "rc": 0,
               "stdout": w.render_manifest(cfg["values"]) + rs.HELM_MANIFEST})
    # -- Artifactory reads
    host = f"https://{cfg['jfrog']['host']}"
    prov = list(w.art.providers.values())
    for p in prov:
        p.setdefault("token_issuer", p["issuer_url"])
    fx.append({"http": f"GET {host}/access/api/v1/oidc", "status": 200, "body": json.dumps(prov)})
    fx.append({"http": f"GET {host}/access/api/v1/oidc/aks-sandbox-oidc/identity_mappings", "status": 200,
               "body": json.dumps([dict(m) for m in w.art.mappings["aks-sandbox-oidc"]])})
    fx.append({"http": f"GET https://{cfg['jfrog']['test_repo_host']}/v2/", "status": 401, "body": "",
               "headers": {"www-authenticate": rs.WWW_AUTH_DEDICATED}})
    fx.append({"http": f"GET {host}/v2/", "status": 401, "body": "", "headers": {"www-authenticate": rs.WWW_AUTH_BARE}})
    fx.append({"http": f"POST {host}/artifactory/api/search/aql", "status": 200, "body": json.dumps({"results": [
        {"repo": "containers-docker-local", "path": "mcp/sonarqube/1.26-v1", "name": "manifest.json"},
        {"repo": "containers-docker-local", "path": "mcp/sonarqube/1.27-v1", "name": "manifest.json"},
        {"repo": "aks-sandbox-test-docker-local", "path": "mcp/sonarqube/1.26-v1", "name": "manifest.json"}]})})
    fx.append({"http": f"GET {host}/artifactory/api/storage/aks-sandbox-test-docker-local/mcp/sonarqube/1.26-v1", "status": 200,
               "body": json.dumps({"repo": "aks-sandbox-test-docker-local", "path": "/mcp/sonarqube/1.26-v1",
                                   "children": [{"uri": "/manifest.json", "folder": False}]})})
    fx.append({"http": f"GET {host}/artifactory/api/storage/aks-sandbox-generic-local/jfrog-credential-provider/1.4.0/jfrog-credential-provider-linux-amd64",
               "status": 200, "body": json.dumps({"size": str(logic.EXPECTED_BINARY_BYTES),
                                                  "checksums": {"sha256": "5c1f0c6a3be2c1d0b0a7c1f3e9d4b2a6c8e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4"}})})
    fx.append({"http": f"GET {host}/access/api/v2/groups", "status": 403, "body": '{"errors":[{"code":"FORBIDDEN","message":"Forbidden"}]}'})
    fx.append({"http": f"GET {host}/artifactory/api/docker/example-docker-remote/v2/library/alpine/manifests/latest", "status": 302,
               "body": "", "headers": {"location": rs.DORMANT_LOCATION}})
    fx.append({"http": f"GET {host}/artifactory/api/system/logs/data?id=artifactory-request.log", "status": 404,
               "body": '{"errors":[{"status":404,"message":"Not Found"}]}'})
    fx.append({"http": f"POST {host}/access/api/v1/oidc", "status": 409, "body": '{"errors":[{"message":"Provider aks-sandbox-oidc already exists"}]}'})
    fx.append({"http": f"POST {host}/access/api/v1/oidc/aks-sandbox-oidc/identity_mappings", "status": 409,
               "body": '{"errors":[{"message":"Identity mapping aks-workload-identity-mapping already exists"}]}'})
    fx.append({"http": f"POST {host}/artifactory/api/docker/containers-docker-local/v2/promote", "status": 200,
               "body": "Promotion ended successfully"})
    fx.append({"http": f"POST {host}/artifactory/api/docker/docker-virtual/v2/promote", "status": 400,
               "body": rs.UNSUPPORTED_VIRTUAL})
    # -- ADO: queue/show/logs for every pilot scenario and template action, defaults and DryRun=false
    fx.append({"argv": ["az", "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken", "-o", "tsv"],
               "rc": 0, "stdout": "replay-mode-ado-bearer\n"})
    last = {}
    for name, kind, params, log, result in steps:
        if kind == "pilot":
            last[name] = log
    canned = canned_logs(cfg, w, last)
    tcfg = cfg
    n = 5000
    variants = []
    for s in knowledge.all_scenarios():
        variants.append(("pilot", s["name"], {}))
        if s["dry_run"]:
            variants.append(("pilot", s["name"], {"DryRun": False}))
    for a in knowledge.TEMPLATE_ACTIONS:
        variants += [("template", a["name"], {}), ("template", a["name"], {"DryRun": False})]
    variants += [("mappings", "Apply", {}), ("mappings", "Apply", {"DryRun": False})]
    for kind, name, params in variants:
        n += 1
        req = build_request(tcfg if kind != "pilot" else cfg, kind, name, params)["request"]
        argv = ["az", "pipelines", "run", "--name", req["pipeline"], "--branch", req["branch"], "--parameters",
                *param_args(req["parameters"]), *scope]
        fx.append({"argv": argv, "rc": 0, "stdout": json.dumps({"id": n, "status": "notStarted",
                                                                  "_links": {"web": {"href": f"{ORG_URL}/{PROJECT}/_build/results?buildId={n}"}}})})
        fx.append({"argv": ["az", "pipelines", "runs", "show", "--id", str(n), *scope], "rc": 0,
                   "stdout": json.dumps({"id": n, "status": "completed", "result": "succeeded", "finishTime": "2026-10-16T15:00:00Z"})})
        base = f"{ORG_URL}/{PROJECT}/_apis/build/builds/{n}/logs"
        fx.append({"http": f"GET {base}?api-version=7.1", "status": 200, "body": json.dumps({"count": 1, "value": [{"id": 1}]})})
        dry = req["parameters"].get("DryRun")
        fx.append({"http": f"GET {base}/1?api-version=7.1", "status": 200, "body": canned(kind, name, dry)})
    for p in (cfg["ado"]["cluster_pipeline"], cfg["ado"]["mappings_pipeline"]):
        fx.append({"argv": ["az", "pipelines", "variable", "list", "--pipeline-name", p, *scope], "rc": 0,
                   "stdout": json.dumps({"JfrogDownloadToken": {"isSecret": True, "value": None, "allowOverride": True}})})
    return fx


def canned_logs(cfg, w, last):
    def canned(kind, name, dry):
        stamp = "2026-10-16T15:00:00.0000000Z "
        if kind == "pilot" and name in last and not (name == "RESET_NODE_JFROG_ENTRY" and dry) and not (
                name == "INSTALL_JFROG_PROVIDER" and dry):
            return last[name]
        if kind == "pilot" and name == "RESET_NODE_JFROG_ENTRY":
            w2 = copy.deepcopy(w)
            w2.reset_node_entries(dry_run=True)
            return "\n".join(stamp + l for l in w2.reset_logs[-1].splitlines())
        if kind == "pilot" and name == "INSTALL_JFROG_PROVIDER":
            body = ["helm upgrade --install jfrog-cred-provider jfrog/jfrog-credential-provider --version 1.4.0 "
                    "-f scripts/jfrog-provider/values-sandbox.yaml --set binaryDownload.auth.accessToken=*** --dry-run=server",
                    "DryRun: the release validated server-side; nothing was changed"]
            return "\n".join(stamp + l for l in body)
        if kind == "pilot" and name == "CREATE_CONTROL_POD":
            w2 = copy.deepcopy(w)
            w2.remove_image()
            p = w2.create_pod(name="control-pull-test", sa="default", node=NODE_G, kind="control")
            return "\n".join(stamp + l for l in p["events"].splitlines())
        if kind == "pilot" and name == "FIND_CREDENTIAL_PROVIDER_WRITERS":
            body = ["===== DaemonSets mounting /var/lib/kubelet =====", f"jfrog/{cfg['k8s']['daemonset']}  hostPath /var/lib/kubelet",
                    "===== JFrog DaemonSets in other namespaces =====", "(none: jfrog-provider/jfrog-credential-provider was deleted 2026-10-16)"]
            return "\n".join(stamp + l for l in body)
        lines = {"CHECK_AKS_NODEPOOL_CONFIG": "nodepool appnodepool: no custom kubelet config, no linuxOSConfig credential-provider changes",
                 "CHECK_NODE_VMSS_EXTENSIONS": "VMSS extensions: vmssCSE, AKSLinuxBilling (no credential-provider writers)",
                 "MIRROR_TEST_IMAGE": "GET https://example-docker-remote.jfrog.io/v2/library/alpine/manifests/latest → 302 https://landing.jfrog.com/reactivate-server/example-docker-remote",
                 "CLEANUP_STALE_JFROG_DAEMONSET": "No JFrog DaemonSet outside jfrog: nothing to delete",
                 "DELETE_TEST_POD": f"{'DryRun: would delete' if dry else 'deleted'} pod jfrog/secretless-pull-test",
                 "UNINSTALL_JFROG_PROVIDER": f"{'DryRun: would run' if dry else 'ran'} helm uninstall jfrog-cred-provider -n jfrog (node entries stay until RESET_NODE_JFROG_ENTRY; untested)",
                 "REMOVE_NODE_LABEL": f"{'DryRun: would run' if dry else 'ran'} kubectl label nodes --all credentialsProviderEnabled-"}
        if kind == "pilot":
            return stamp + lines.get(name, f"{name} finished")
        if kind == "mappings":
            return stamp + f"mappings/{cfg['cluster']['name']}.json: 1 mapping, 0 to create, 0 to update, 0 to prune (Prune=false){' [DryRun]' if dry else ''}"
        if name == "Verify":
            return last["CREATE_TEST_POD"]
        if name == "Preflight":
            return stamp + "OIDC issuer: enabled. Workload Identity: enabled. Foreign JFrog provider DaemonSets: none. Existing release: jfrog-cred-provider (rev 15). Sampled nodes: azure provider entry present."
        return stamp + f"{name}{' (DryRun: nothing changed)' if dry else ''} finished"
    return canned


def build() -> dict:
    """{file name: content} for every generated fixture file."""
    cfg, w, steps = story()
    return {"shell.json": json.dumps(shell_fixtures(cfg, w, steps), indent=1) + "\n",
            "config.seed.json": json.dumps(cfg, indent=2) + "\n",
            "history.seed.json": json.dumps(history(cfg, w, steps), indent=1) + "\n"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in build().items():
        (OUT / name).write_text(text)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
