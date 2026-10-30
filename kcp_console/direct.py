"""Direct execution of jfrog/jfrog-credentials-provider AZURE.md, Option B (Workload Identity,
projected service-account tokens), with the operator's az login, kubeconfig and JFROG_ACCESS_TOKEN.

plan() lists the steps with the exact commands and requests, so the approval covers them.
execute() runs them in order. With dry_run, nothing changes anywhere: kubectl and helm use
--dry-run=server, az aks update and the JFrog writes are skipped (their request is shown).
Every step is idempotent: what is already in place is left alone and reported.

Steps (AZURE.md section in brackets):
  cluster     read the cluster: OIDC issuer and Workload Identity (4B.1)
  oidc        az aks update --enable-oidc-issuer, only when it is off (4B.1)
  kubeconfig  az aks get-credentials into the console's own kubeconfig
  sa          namespace + service account annotated JFrogExchange="true" (4B.2)
  provider    JFrog OIDC provider, issuer = the cluster's (4B.3)
  mapping     identity mapping on system:serviceaccount:<ns>:<sa> (4B.3)
  label       label the nodes the chart's nodeAffinity selects (Step 5)
  helm        helm upgrade --install jfrog-credential-provider (Step 5)
  daemonset   the DaemonSet is ready on every labelled node (Verification)
  pull        a pod with imagePullSecrets: [] pulls the test image (Verification)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import model, redact
from .jfrog import Jfrog

CHART_REPO = "https://charts.jfrog.io"
CHART = "jfrog-credential-provider"
STEPS = ("cluster", "oidc", "kubeconfig", "sa", "provider", "mapping", "label", "helm", "daemonset", "pull")
VERIFY_STEPS = ("cluster", "kubeconfig", "daemonset", "pull")
TEARDOWN_STEPS = ("kubeconfig", "uninstall", "unlabel", "delete-mapping", "delete-provider")


class StepError(RuntimeError):
    pass


def paths(root: Path, name: str) -> dict:
    d = Path(root) / "kube"
    return {"dir": d, "kubeconfig": d / f"{name}.kubeconfig", "values": d / f"{name}-values.yaml",
            "sa": d / f"{name}-sa.json", "pod": d / f"{name}-pull-pod.json"}


def _az(cfg):
    c = cfg["cluster"]
    return ["--resource-group", c["resource_group"], "--name", c["name"]] + (["--subscription", c["subscription_id"]] if c["subscription_id"] else [])


def sa_manifest(cfg) -> dict:
    k = cfg["k8s"]
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": k["namespace"]}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": k["service_account"], "namespace": k["namespace"], "annotations": {"JFrogExchange": "true"}}}]}


def pod_manifest(cfg) -> dict:
    k = cfg["k8s"]
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": k["test_pod"], "namespace": k["namespace"], "labels": {"app.kubernetes.io/managed-by": "kcp-console"}},
            "spec": {"serviceAccountName": k["service_account"], "imagePullSecrets": [], "restartPolicy": "Never",
                     "nodeSelector": {cfg["values"]["node_affinity_label"]: "true"},
                     "containers": [{"name": "pull", "image": cfg["jfrog"]["test_image"], "command": ["sh", "-c", "echo pulled; sleep 5"],
                                     "imagePullPolicy": "Always"}]}}


def _resolved(cfg: dict) -> dict:
    cfg = model.merged(cfg)
    cfg["values"]["provider_name"] = model.provider_name(cfg)
    return cfg


def plan(cfg: dict, root: Path, which: str = "setup", dry_run: bool = True) -> list:
    """The steps with the exact commands Runner runs, for the approval screen. kubectl and helm get the
    console's kubeconfig through KUBECONFIG. No secrets: the JFrog token is only ever a header."""
    cfg = _resolved(cfg)
    p = paths(root, cfg["name"])
    k, j, v = cfg["k8s"], cfg["jfrog"], cfg["values"]
    kc = []
    srv = ["--dry-run=server"] if dry_run else []
    label_sel = f"{v['node_affinity_label']}=true"
    nodes = cfg["cluster"]["nodes"] or ["--all"]
    host = j["host"]
    S = {
        "cluster": ("Read the cluster", "read", ["az", "aks", "show", *_az(cfg), "-o", "json"]),
        "oidc": ("Enable the OIDC issuer (only if off)", "change", ["az", "aks", "update", *_az(cfg), "--enable-oidc-issuer"]),
        "kubeconfig": ("Get cluster credentials", "local", ["az", "aks", "get-credentials", *_az(cfg), "--file", str(p["kubeconfig"]), "--overwrite-existing"]),
        "sa": ("Namespace and service account (JFrogExchange: \"true\")", "change", ["kubectl", *kc, "apply", "-f", str(p["sa"]), *srv]),
        "provider": (f"JFrog OIDC provider {v['provider_name']}", "change", f"POST https://{host}/access/api/v1/oidc  {json.dumps(model.provider_request(cfg))}"),
        "mapping": (f"Identity mapping {j['mapping_name']}", "change",
                    f"POST https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings  {json.dumps(model.mapping_request(cfg))}"),
        "label": (f"Label nodes {label_sel}", "change", ["kubectl", *kc, "label", "nodes", *nodes, label_sel, "--overwrite", *srv]),
        "helm": (f"helm upgrade --install {k['release']}", "change",
                 ["helm", "upgrade", "--install", k["release"], CHART, "--repo", CHART_REPO, "--version", v["chart_version"],
                  "--namespace", k["namespace"], "--create-namespace", "-f", str(p["values"]),
                  *(["--dry-run=server"] if dry_run else [])]),
        "daemonset": ("DaemonSet ready on the labelled nodes", "read", ["kubectl", *kc, "get", "daemonset", "-n", k["namespace"], "-o", "json"]),
        "pull": (f"Test pull of {j['test_image'] or '<test image>'}", "change", ["kubectl", *kc, "apply", "-f", str(p["pod"]), *srv]),
        "uninstall": (f"helm uninstall {k['release']}", "change", ["helm", "uninstall", k["release"], "--namespace", k["namespace"],
                                                                  *(["--dry-run"] if dry_run else [])]),
        "unlabel": (f"Remove the label {v['node_affinity_label']}", "change", ["kubectl", *kc, "label", "nodes", "--all", f"{v['node_affinity_label']}-", *srv]),
        "delete-mapping": (f"Delete identity mapping {j['mapping_name']}", "change",
                           f"DELETE https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings/{j['mapping_name']}"),
        "delete-provider": (f"Delete OIDC provider {v['provider_name']}", "change", f"DELETE https://{host}/access/api/v1/oidc/{v['provider_name']}"),
    }
    ids = {"setup": STEPS, "verify": VERIFY_STEPS, "teardown": TEARDOWN_STEPS}[which]
    out = []
    for i in ids:
        title, kind, cmd = S[i]
        skipped_in_dry = dry_run and i in ("oidc", "provider", "mapping", "delete-mapping", "delete-provider")
        out.append({"id": i, "title": title, "kind": kind, "command": cmd if isinstance(cmd, str) else " ".join(cmd),
                    "dry_run_note": "shown, not sent: this call has no dry run" if skipped_in_dry else ""})
    return out


def problems(cfg: dict, which: str = "setup") -> list:
    """Missing inputs that would make a step fail, in plain words."""
    cfg = model.merged(cfg)
    c, j, v = cfg["cluster"], cfg["jfrog"], cfg["values"]
    need = [("cluster.name", c["name"]), ("cluster.resource_group", c["resource_group"])]
    if which != "verify":
        need += [("jfrog.host", j["host"]), ("jfrog.mapping_name", j["mapping_name"]), ("jfrog.mapped_user", j["mapped_user"]),
                 ("values.match_images", v["match_images"])]
    if which != "teardown":
        need += [("jfrog.test_image", j["test_image"])]
    return [f"{f} is empty" for f, val in need if not val]


class Runner:
    """Runs a plan. log(text) gets one redacted line per event."""

    def __init__(self, cfg, sh, root, env, log, dry_run=True, sleep=time.sleep, pull_timeout=180):
        self.cfg = _resolved(cfg)
        self.sh, self.env, self.log, self.dry, self.sleep = sh, env, log, dry_run, sleep
        self.p = paths(root, self.cfg["name"])
        self.p["dir"].mkdir(parents=True, exist_ok=True, mode=0o700)
        self.kenv = {"KUBECONFIG": str(self.p["kubeconfig"])}
        self.jf = Jfrog(sh, self.cfg["jfrog"]["host"], env=env)
        self.facts = {}
        self.pull_timeout = pull_timeout

    def _run(self, argv, timeout=300, ok_rcs=(0,)):
        r = self.sh.run(argv, timeout=timeout, extra_env=self.kenv)
        if r.rc not in ok_rcs:
            raise StepError(redact.text((r.stderr or r.stdout or f"exit {r.rc}").strip().splitlines()[-1] if (r.stderr or r.stdout) else f"exit {r.rc}"))
        return r

    def execute(self, steps: list, stop=lambda: False) -> list:
        results = []
        for st in steps:
            if stop():
                self.log("■ stopped before " + st["title"])
                break
            fn = getattr(self, "s_" + st["id"].replace("-", "_"))
            self.log(f"▶ {st['title']}")
            try:
                res = fn()
                status, detail = res if isinstance(res, tuple) else ("ok", res)
            except StepError as exc:
                results.append({"id": st["id"], "status": "failed", "detail": str(exc)})
                self.log(f"✗ {st['title']}: {exc}")
                break
            results.append({"id": st["id"], "status": status, "detail": detail})
            self.log(f"{'✓' if status in ('ok', 'done') else '•'} {st['title']}: {detail}")
        return results

    # -- steps -----------------------------------------------------------------

    def s_cluster(self):
        r = self._run(["az", "aks", "show", *_az(self.cfg), "-o", "json"], timeout=120)
        d = r.json({}) or {}
        prof = d.get("oidcIssuerProfile") or {}
        self.facts.update(issuer=prof.get("issuerUrl") or "", oidc=bool(prof.get("enabled")),
                          wi=bool(((d.get("securityProfile") or {}).get("workloadIdentity") or {}).get("enabled")),
                          aad=bool(d.get("aadProfile")), version=d.get("kubernetesVersion", ""))
        if self.facts["issuer"]:
            self.cfg["cluster"]["oidc_issuer"] = self.facts["issuer"]
        return "ok", (f"Kubernetes {self.facts['version']}; OIDC issuer {'on' if self.facts['oidc'] else 'off'}"
                      f"{' (' + self.facts['issuer'] + ')' if self.facts['issuer'] else ''}; Workload Identity {'on' if self.facts['wi'] else 'off'}")

    def s_oidc(self):
        if self.facts.get("oidc"):
            return "ok", "already enabled"
        if self.dry:
            return "skipped", "dry run: az aks update --enable-oidc-issuer would run"
        self._run(["az", "aks", "update", *_az(self.cfg), "--enable-oidc-issuer"], timeout=1800)
        self.s_cluster()
        return "done", f"enabled; issuer {self.facts.get('issuer')}"

    def s_kubeconfig(self):
        self._run(["az", "aks", "get-credentials", *_az(self.cfg), "--file", str(self.p["kubeconfig"]), "--overwrite-existing"], timeout=120)
        self.p["kubeconfig"].chmod(0o600)
        if self.facts.get("aad"):
            r = self.sh.run(["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", str(self.p["kubeconfig"])],
                            timeout=60, extra_env=self.kenv)
            if not r.ok:
                raise StepError("this cluster uses Entra ID auth: install kubelogin (brew install Azure/kubelogin/kubelogin)")
        return "ok", f"kubeconfig at {self.p['kubeconfig'].name}"

    def _dry_flag(self):
        """--dry-run=server, or =client while the namespace does not exist yet: the API server cannot place an
        object in a namespace that the same dry run only pretends to create."""
        if "ns" not in self.facts:
            ns = self.cfg["k8s"]["namespace"]
            self.facts["ns"] = self.sh.run(["kubectl", "get", "namespace", ns, "-o", "json"], timeout=60, extra_env=self.kenv).rc == 0
        return "--dry-run=server" if self.facts["ns"] else "--dry-run=client"

    def s_sa(self):
        self.p["sa"].write_text(json.dumps(sa_manifest(self.cfg), indent=2))
        flag = self._dry_flag() if self.dry else None
        r = self._run(["kubectl", "apply", "-f", str(self.p["sa"]), *([flag] if flag else [])])
        out = " ".join(r.stdout.split())[:300]
        if flag == "--dry-run=client":
            out = f"namespace {self.cfg['k8s']['namespace']} is created on the real run; checked client-side: {out}"
        return ("ok" if self.dry else "done"), out

    def _provider_state(self):
        r = self.jf.providers()
        if not r["ok"]:
            raise StepError(f"GET /access/api/v1/oidc: HTTP {r['status']} {r['error']}".strip())
        return {p.get("name"): p for p in (r["json"] or [])}

    def s_provider(self):
        cfg = self.cfg
        name = cfg["values"]["provider_name"]
        if name in cfg["jfrog"]["protected_providers"]:
            raise StepError(f"{name} is protected; pick another provider name")
        issuer = cfg["cluster"]["oidc_issuer"]
        if not issuer:
            if self.dry:
                return "skipped", "dry run: created once the OIDC issuer is on (the oidc step enables it)"
            raise StepError("no OIDC issuer yet (the cluster step reads it)")
        if not self.jf.token_present:
            if self.dry:
                return "skipped", f"dry run: no JFrog token, so the provider was not read; would create {name} for {issuer}"
            raise StepError("no JFrog token: start the console with JFROG_ACCESS_TOKEN")
        have = self._provider_state().get(name)
        if have:
            if (have.get("issuer_url") or "").rstrip("/") != issuer.rstrip("/"):
                raise StepError(f"{name} exists with issuer {have.get('issuer_url')}, not this cluster's {issuer}")
            return "ok", "exists with this cluster's issuer"
        if self.dry:
            return "skipped", "dry run: would create it"
        r = self.jf.create_provider(model.provider_request(cfg))
        if not r["ok"]:
            raise StepError(f"create provider: HTTP {r['status']} {redact.text(r['body'] or '')[:200]}")
        return "done", "created"

    def s_mapping(self):
        cfg = self.cfg
        prov, name = cfg["values"]["provider_name"], cfg["jfrog"]["mapping_name"]
        if not self.jf.token_present:
            if self.dry:
                return "skipped", f"dry run: no JFrog token; would map {model.subject(cfg)} → {cfg['jfrog']['mapped_user']}"
            raise StepError("no JFrog token: start the console with JFROG_ACCESS_TOKEN")
        r = self.jf.mappings(prov)
        if r["ok"] and any(m.get("name") == name for m in (r["json"] or [])):
            return "ok", "exists"
        if self.dry:
            return "skipped", f"dry run: would map {model.subject(cfg)} → {cfg['jfrog']['mapped_user']}"
        r = self.jf.create_mapping(prov, model.mapping_request(cfg))
        if not r["ok"]:
            raise StepError(f"create mapping: HTTP {r['status']} {redact.text(r['body'] or '')[:200]}")
        return "done", f"{model.subject(cfg)} → {cfg['jfrog']['mapped_user']}"

    def s_label(self):
        lab = f"{self.cfg['values']['node_affinity_label']}=true"
        nodes = self.cfg["cluster"]["nodes"] or ["--all"]
        r = self._run(["kubectl", "label", "nodes", *nodes, lab, "--overwrite", *(["--dry-run=server"] if self.dry else [])])
        return ("ok" if self.dry else "done"), f"{len(r.stdout.strip().splitlines())} node(s) labelled {lab}"

    def s_helm(self):
        self.p["values"].write_text(model.render_values(self.cfg))
        k, v = self.cfg["k8s"], self.cfg["values"]
        self._run(["helm", "upgrade", "--install", k["release"], CHART, "--repo", CHART_REPO, "--version", v["chart_version"],
                   "--namespace", k["namespace"], "--create-namespace", "-f", str(self.p["values"]),
                   *(["--dry-run=server"] if self.dry else [])], timeout=600)
        return ("ok" if self.dry else "done"), f"{CHART} {v['chart_version']} {'rendered and validated by the API server' if self.dry else 'installed'}"

    def s_daemonset(self):
        if self.dry:
            return "skipped", "dry run: checked after a real install"
        ns = self.cfg["k8s"]["namespace"]
        for _ in range(30):
            d = self._run(["kubectl", "get", "daemonset", "-n", ns, "-o", "json"]).json({}) or {}
            ds = [x for x in d.get("items", []) if "jfrog" in x["metadata"]["name"]]
            if ds:
                st = ds[0].get("status", {})
                want, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
                if want and ready == want:
                    return "ok", f"{ds[0]['metadata']['name']}: {ready}/{want} ready"
            self.sleep(10)
        raise StepError(f"DaemonSet not ready: {ready if ds else 0}/{want if ds else 0}")

    def s_pull(self):
        k = self.cfg["k8s"]
        self.p["pod"].write_text(json.dumps(pod_manifest(self.cfg), indent=2))
        if self.dry:
            flag = self._dry_flag()
            self._run(["kubectl", "apply", "-f", str(self.p["pod"]), flag])
            return "skipped", f"dry run: pod validated {'by the API server' if flag.endswith('server') else 'client-side'}"
        self._run(["kubectl", "delete", "pod", k["test_pod"], "-n", k["namespace"], "--ignore-not-found", "--wait=true"], timeout=120)
        self._run(["kubectl", "apply", "-f", str(self.p["pod"])])
        waited, phase, reason = 0, "", ""
        while waited < self.pull_timeout:
            d = self._run(["kubectl", "get", "pod", k["test_pod"], "-n", k["namespace"], "-o", "json"]).json({}) or {}
            phase = (d.get("status") or {}).get("phase", "")
            cs = ((d.get("status") or {}).get("containerStatuses") or [{}])[0]
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
            if phase in ("Running", "Succeeded") or reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
                break
            self.sleep(5)
            waited += 5
        ev = self._run(["kubectl", "get", "events", "-n", k["namespace"], "--field-selector", f"involvedObject.name={k['test_pod']}", "-o", "json"]).json({}) or {}
        msgs = [e.get("message", "") for e in ev.get("items", []) if e.get("reason") in ("Pulled", "Failed", "BackOff")]
        self._run(["kubectl", "delete", "pod", k["test_pod"], "-n", k["namespace"], "--ignore-not-found", "--wait=false"])
        if phase in ("Running", "Succeeded"):
            return "done", "pulled with imagePullSecrets: [] — " + (msgs[0] if msgs else phase)
        raise StepError(f"pull failed ({reason or phase or 'timeout'}): {msgs[-1] if msgs else 'no event'}")

    def s_uninstall(self):
        k = self.cfg["k8s"]
        self._run(["helm", "uninstall", k["release"], "--namespace", k["namespace"], *(["--dry-run"] if self.dry else [])], ok_rcs=(0, 1))
        return ("ok" if self.dry else "done"), "release removed" if not self.dry else "dry run"

    def s_unlabel(self):
        lab = self.cfg["values"]["node_affinity_label"]
        self._run(["kubectl", "label", "nodes", "--all", f"{lab}-", *(["--dry-run=server"] if self.dry else [])])
        return ("ok" if self.dry else "done"), f"label {lab} removed"

    def s_delete_mapping(self):
        if self.dry:
            return "skipped", "dry run"
        r = self.jf.delete_mapping(self.cfg["values"]["provider_name"], self.cfg["jfrog"]["mapping_name"])
        return ("done" if r["ok"] or r["status"] == 404 else "failed"), f"HTTP {r['status']}"

    def s_delete_provider(self):
        name = self.cfg["values"]["provider_name"]
        if name in self.cfg["jfrog"]["protected_providers"]:
            raise StepError(f"{name} is protected")
        if self.dry:
            return "skipped", "dry run"
        r = self.jf.delete_provider(name)
        return ("done" if r["ok"] or r["status"] == 404 else "failed"), f"HTTP {r['status']}"
