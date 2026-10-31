"""Direct execution of jfrog/jfrog-credentials-provider AZURE.md, Option B (Workload Identity,
projected service-account tokens), with the operator's az login, kubeconfig and a short-lived JFrog token minted by OIDC exchange (jfrog.OperatorToken).

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

The OIDC proof (verify, and the end of setup), every link read from the cluster or JFrog:
  sa-check    the service account carries JFrogExchange="true"
  trust       the JFrog provider's issuer is this cluster's issuer
  map         the identity mapping maps this service account's subject to the mapped user
  anon        the test image without credentials: 401 (else a pull proves nothing)
  pull        the pull with no pull secret
  who         JFrog's download stats: the image was last downloaded by the mapped user
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import model, platforms, redact
from .jfrog import Jfrog

CHART_REPO = "https://charts.jfrog.io"
CHART = "jfrog-credential-provider"
STEPS = ("cluster", "oidc", "kubeconfig", "sa", "provider", "mapping", "label", "helm", "daemonset", "anon", "pull", "who")
VERIFY_STEPS = ("cluster", "kubeconfig", "sa-check", "trust", "map", "daemonset", "anon", "pull", "who")
PROOF_STEPS = ("cluster", "sa-check", "trust", "map", "anon", "pull", "who")
TEARDOWN_STEPS = ("kubeconfig", "uninstall", "unlabel", "delete-mapping", "delete-provider", "gone")
# GKE adds the Google service account that the pod's service account acts as (GCP.md Step 2B).
GKE_STEPS = STEPS[:3] + ("gsa",) + STEPS[3:]


def step_ids(cfg: dict, which: str) -> tuple:
    if which == "setup":
        return GKE_STEPS if platforms.kind(cfg) == "gke" else STEPS
    return {"verify": VERIFY_STEPS, "teardown": TEARDOWN_STEPS}[which]


class StepError(RuntimeError):
    pass


def _cause(event: str) -> str:
    """The reason in a kubelet event, without image references or URLs."""
    if "oauth token" in event.lower() and "401" in event:
        return "registry token request got 401: cached or expired credentials"
    for k in ("401 Unauthorized", "403 Forbidden", "404 Not Found", "not found", "no basic auth credentials", "timeout"):
        if k.lower() in event.lower():
            return k
    return "see the cluster's events"


def paths(root: Path, name: str) -> dict:
    d = Path(root) / "kube"
    return {"dir": d, "kubeconfig": d / f"{name}.kubeconfig", "values": d / f"{name}-values.yaml",
            "sa": d / f"{name}-sa.json", "pod": d / f"{name}-pull-pod.json"}


def sa_manifest(cfg) -> dict:
    k = cfg["k8s"]
    ann = {"JFrogExchange": "true"}
    if platforms.kind(cfg) == "gke":
        ann["iam.gke.io/gcp-service-account"] = platforms.gsa_email(cfg)
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": k["namespace"]}},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": k["service_account"], "namespace": k["namespace"], "annotations": ann}}]}


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
    if platforms.kind(cfg) == "gke":
        cfg["cluster"]["oidc_issuer"] = platforms.issuer(cfg)
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
        "cluster": ("Read the cluster", "read", platforms.show(cfg)),
        "oidc": (platforms.enable_title(cfg), "change", platforms.enable(cfg)),
        "kubeconfig": ("Get cluster credentials", "local", platforms.credentials(cfg, p["kubeconfig"])),
        "gsa": (f"Google service account {platforms.gsa_email(cfg) if platforms.kind(cfg) == 'gke' else ''}", "change",
                "gcloud iam service-accounts describe/create; add-iam-policy-binding "
                + ", ".join(platforms.GKE_ROLES) + (f" for {platforms.wi_member(cfg)}" if platforms.kind(cfg) == "gke" else "")),
        "sa": ("Namespace and service account", "change", ["kubectl", *kc, "apply", "-f", str(p["sa"]), *srv]),
        "provider": (f"JFrog OIDC provider {v['provider_name']}", "change", f"POST https://{host}/access/api/v1/oidc  {json.dumps(model.provider_request(cfg))}"),
        "mapping": (f"Identity mapping {j['mapping_name']}", "change",
                    f"POST https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings  {json.dumps(model.mapping_request(cfg))}"),
        "label": ("Label the nodes", "change", ["kubectl", *kc, "label", "nodes", *nodes, label_sel, "--overwrite", *srv]),
        "helm": (f"Install the credential provider chart {v['chart_version']}", "change",
                 ["helm", "upgrade", "--install", k["release"], CHART, "--repo", CHART_REPO, "--version", v["chart_version"],
                  "--namespace", k["namespace"], "--create-namespace", "-f", str(p["values"]),
                  *(["--dry-run=server"] if dry_run else [])]),
        "daemonset": ("DaemonSet ready on the labelled nodes", "read", ["kubectl", *kc, "get", "daemonset", "-n", k["namespace"], "-o", "json"]),
        "pull": ("Test pull without a pull secret", "change", ["kubectl", *kc, "apply", "-f", str(p["pod"]), *srv]),
        "sa-check": ("Service account allowed to exchange", "read", ["kubectl", *kc, "get", "serviceaccount", k["service_account"], "-n", k["namespace"], "-o", "json"]),
        "trust": ("JFrog trusts this cluster's issuer", "read", f"GET https://{host}/access/api/v1/oidc"),
        "map": (f"Service account mapped to {j['mapped_user']}", "read", f"GET https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings"),
        "anon": ("Image refused without credentials", "read", f"GET https://{host}/v2/... (no credentials)"),
        "who": (f"JFrog saw the pull as {j['mapped_user']}", "read", f"GET https://{host}/artifactory/api/storage/...?stats"),
        "uninstall": ("Uninstall the credential provider", "change", ["helm", "uninstall", k["release"], "--namespace", k["namespace"],
                                                                  *(["--dry-run"] if dry_run else [])]),
        "unlabel": (f"Remove the label {v['node_affinity_label']}", "change", ["kubectl", *kc, "label", "nodes", "--all", f"{v['node_affinity_label']}-", *srv]),
        "delete-mapping": (f"Delete identity mapping {j['mapping_name']}", "change",
                           f"DELETE https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings/{j['mapping_name']}"),
        "delete-provider": (f"Delete OIDC provider {v['provider_name']}", "change", f"DELETE https://{host}/access/api/v1/oidc/{v['provider_name']}"),
        "gone": ("Nothing left on the cluster or JFrog", "read", f"helm status; kubectl get daemonset, nodes; GET https://{host}/access/api/v1/oidc"),
    }
    ids = step_ids(cfg, which)
    out = []
    for i in ids:
        title, kind, cmd = S[i]
        skipped_in_dry = dry_run and i in ("oidc", "gsa", "provider", "mapping", "delete-mapping", "delete-provider")
        out.append({"id": i, "title": title, "kind": kind, "command": cmd if isinstance(cmd, str) else " ".join(cmd),
                    "dry_run_note": "shown, not sent: this call has no dry run" if skipped_in_dry else ""})
    return out


def problems(cfg: dict, which: str = "setup") -> list:
    """Missing inputs that would make a step fail, in plain words."""
    cfg = model.merged(cfg)
    c, j, v = cfg["cluster"], cfg["jfrog"], cfg["values"]
    need = platforms.required(cfg)
    if which != "verify":
        need += [("jfrog.host", j["host"]), ("jfrog.mapping_name", j["mapping_name"]), ("jfrog.mapped_user", j["mapped_user"]),
                 ("values.match_images", v["match_images"])]
    if which != "teardown":
        need += [("jfrog.test_image", j["test_image"])]
    return [f"{f} is empty" for f, val in need if not val]


class Runner:
    """Runs a plan. log(text) gets one redacted line per event."""

    def __init__(self, cfg, sh, root, env, log, dry_run=True, sleep=time.sleep, pull_timeout=180, clock=time.time, tokens=None):
        self.cfg = _resolved(cfg)
        self.sh, self.env, self.log, self.dry, self.sleep = sh, env, log, dry_run, sleep
        self.p = paths(root, self.cfg["name"])
        self.p["dir"].mkdir(parents=True, exist_ok=True, mode=0o700)
        self.kenv = {"KUBECONFIG": str(self.p["kubeconfig"])}
        self.jf = Jfrog(sh, self.cfg["jfrog"]["host"], tokens=tokens)
        self.facts = {}
        self.pull_timeout = pull_timeout
        self.clock = clock

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
                results.append({"id": st["id"], "title": st["title"], "status": "failed", "detail": str(exc)})
                self.log(f"✗ {st['title']}: {exc}")
                break
            results.append({"id": st["id"], "title": st["title"], "status": status, "detail": detail})
            self.log(f"{'✓' if status in ('ok', 'done') else '•'} {st['title']}: {detail}")
        return results

    # -- steps -----------------------------------------------------------------

    def s_cluster(self):
        r = self._run(platforms.show(self.cfg), timeout=120)
        self.facts.update(platforms.parse(self.cfg, r.json({}) or {}))
        if self.facts["issuer"]:
            self.cfg["cluster"]["oidc_issuer"] = self.facts["issuer"]
        if self.facts.get("autopilot"):
            raise StepError("GKE Autopilot does not allow the provider's DaemonSet on its nodes: use a Standard cluster")
        if platforms.kind(self.cfg) == "gke":
            return "ok", f"Kubernetes {self.facts['version']}; Workload Identity {'on' if self.facts['wi'] else 'off'}"
        return "ok", (f"Kubernetes {self.facts['version']}; OIDC issuer {'on' if self.facts['oidc'] else 'off'}; "
                      f"Workload Identity {'on' if self.facts['wi'] else 'off'}")

    def s_oidc(self):
        if platforms.enabled(self.cfg, self.facts):
            return "ok", "already enabled"
        if self.dry:
            return "skipped", "dry run: would enable it"
        self._run(platforms.enable(self.cfg), timeout=1800)
        self.s_cluster()
        return "done", "enabled"

    def s_kubeconfig(self):
        if platforms.kind(self.cfg) == "gke" and self.sh.run(["gke-gcloud-auth-plugin", "--version"], timeout=30).rc != 0:
            raise StepError("kubectl needs gke-gcloud-auth-plugin for GKE: gcloud components install gke-gcloud-auth-plugin")
        self._run(platforms.credentials(self.cfg, self.p["kubeconfig"]), timeout=120)
        self.p["kubeconfig"].chmod(0o600)
        if self.facts.get("aad"):
            r = self.sh.run(["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", str(self.p["kubeconfig"])],
                            timeout=60, extra_env=self.kenv)
            if not r.ok:
                raise StepError("this cluster uses Entra ID auth: install kubelogin (brew install Azure/kubelogin/kubelogin)")
        return "ok", "ready"

    def s_gsa(self):
        """Adopt the Google service account and its two bindings where they exist; create only what is missing."""
        c = self.cfg["cluster"]
        email, member, proj = platforms.gsa_email(self.cfg), platforms.wi_member(self.cfg), c["project_id"]
        exists = self.sh.run(["gcloud", "iam", "service-accounts", "describe", email, "--project", proj, "--format=json"], timeout=60).rc == 0
        have = set()
        if exists:
            pol = self._run(["gcloud", "iam", "service-accounts", "get-iam-policy", email, "--project", proj, "--format=json"], timeout=60).json({}) or {}
            have = {b.get("role") for b in pol.get("bindings") or [] if member in (b.get("members") or [])}
        missing = [r for r in platforms.GKE_ROLES if r not in have]
        if exists and not missing:
            return "ok", "exists with both bindings"
        if self.dry:
            todo = ([] if exists else ["would create it"]) + ([f"would bind {', '.join(r.split('/')[-1] for r in missing)}"] if missing else [])
            return "skipped", "dry run: " + "; ".join(todo)
        if not exists:
            if not email.endswith(f"@{proj}.iam.gserviceaccount.com"):
                raise StepError(f"{email} does not exist and is not in project {proj}: create it there or leave the field blank")
            self._run(["gcloud", "iam", "service-accounts", "create", email.split("@")[0], "--project", proj,
                       "--display-name", "JFrog kubelet credential provider"], timeout=120)
        for role in missing:
            self._run(["gcloud", "iam", "service-accounts", "add-iam-policy-binding", email, "--project", proj,
                       "--role", role, "--member", member, "--format=none"], timeout=120)
        return "done", ("created; " if not exists else "") + f"{len(missing)} binding(s) added"

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
        if flag == "--dry-run=client":
            return "ok", "valid; the namespace is created on the real run"
        return ("ok" if self.dry else "done"), "valid" if self.dry else "ready"

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
        err = self._jfrog_error()
        if err:
            if self.dry:
                return "skipped", f"dry run: {err}, so the provider was not read; would create {name}"
            raise StepError(err)
        have = self._provider_state().get(name)
        if have:
            if (have.get("issuer_url") or "").rstrip("/") != issuer.rstrip("/"):
                raise StepError(f"{name} trusts another cluster's issuer")
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
        err = self._jfrog_error()
        if err:
            if self.dry:
                return "skipped", f"dry run: {err}; would map {cfg['k8s']['service_account']} → {cfg['jfrog']['mapped_user']}"
            raise StepError(err)
        r = self.jf.mappings(prov)
        if r["ok"] and any(m.get("name") == name for m in (r["json"] or [])):
            return "ok", "exists"
        if self.dry:
            return "skipped", f"dry run: would map {cfg['k8s']['service_account']} → {cfg['jfrog']['mapped_user']}"
        r = self.jf.create_mapping(prov, model.mapping_request(cfg))
        if not r["ok"]:
            raise StepError(f"create mapping: HTTP {r['status']} {redact.text(r['body'] or '')[:200]}")
        return "done", f"{cfg['k8s']['service_account']} → {cfg['jfrog']['mapped_user']}"

    def s_label(self):
        lab = f"{self.cfg['values']['node_affinity_label']}=true"
        nodes = self.cfg["cluster"]["nodes"] or ["--all"]
        r = self._run(["kubectl", "label", "nodes", *nodes, lab, "--overwrite", *(["--dry-run=server"] if self.dry else [])])
        return ("ok" if self.dry else "done"), f"{len(r.stdout.strip().splitlines())} node(s)"

    def s_helm(self):
        self.p["values"].write_text(model.render_values(self.cfg))
        k, v = self.cfg["k8s"], self.cfg["values"]
        self._run(["helm", "upgrade", "--install", k["release"], CHART, "--repo", CHART_REPO, "--version", v["chart_version"],
                   "--namespace", k["namespace"], "--create-namespace", "-f", str(self.p["values"]),
                   *(["--dry-run=server"] if self.dry else [])], timeout=600)
        if self.dry:
            return "ok", f"{CHART} {v['chart_version']} rendered and validated by the API server"
        # An upgrade with the same pod template leaves the old pods in place, so the nodes keep the old
        # provider config and kubelet keeps its cached tokens. Restart them, as the runbook's install does.
        d = self._run(["kubectl", "get", "daemonset", "-n", k["namespace"], "-o", "json"]).json({}) or {}
        names = [x["metadata"]["name"] for x in d.get("items", []) if "jfrog" in x["metadata"]["name"]]
        for n in names:
            self._run(["kubectl", "rollout", "restart", f"daemonset/{n}", "-n", k["namespace"]])
            self._run(["kubectl", "rollout", "status", f"daemonset/{n}", "-n", k["namespace"], "--timeout=300s"], timeout=320)
        return "done", f"{CHART} {v['chart_version']} installed; provider pods restarted"

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
                    return "ok", f"{ready}/{want} ready"
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
        self.facts["pull_started"] = self.clock()
        self._run(["kubectl", "apply", "-f", str(self.p["pod"])])
        waited, phase, reason, uid = 0, "", "", ""
        while waited < self.pull_timeout:
            d = self._run(["kubectl", "get", "pod", k["test_pod"], "-n", k["namespace"], "-o", "json"]).json({}) or {}
            uid = (d.get("metadata") or {}).get("uid", uid)
            phase = (d.get("status") or {}).get("phase", "")
            cs = ((d.get("status") or {}).get("containerStatuses") or [{}])[0]
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
            if phase in ("Running", "Succeeded") or reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
                break
            self.sleep(5)
            waited += 5
        ev = self._run(["kubectl", "get", "events", "-n", k["namespace"], "--field-selector", f"involvedObject.name={k['test_pod']}", "-o", "json"]).json({}) or {}
        # Events outlive a deleted pod; an earlier test pod with the same name is told apart by its uid.
        msgs = [e.get("message", "") for e in ev.get("items", []) if e.get("reason") in ("Pulled", "Failed", "BackOff")
                and (not uid or (e.get("involvedObject") or {}).get("uid") in (None, uid))]
        self._run(["kubectl", "delete", "pod", k["test_pod"], "-n", k["namespace"], "--ignore-not-found", "--wait=false"])
        if phase in ("Running", "Succeeded"):
            return "done", "pulled with no pull secret"
        raise StepError(f"pull failed ({reason or phase or 'timeout'}){': ' + _cause(msgs[-1]) if msgs else ''}")

    # -- the OIDC proof ------------------------------------------------------------

    def _jfrog_error(self) -> str:
        """"" once a short-lived JFrog token is minted, else why not."""
        err = self.jf.token_error()
        return f"no JFrog token ({err})" if err else ""

    def _need_token(self, what):
        err = self._jfrog_error()
        if not err:
            return None
        if self.dry:
            return "skipped", f"dry run: {err}, so {what} was not read"
        raise StepError(err)

    def s_sa_check(self):
        k = self.cfg["k8s"]
        r = self.sh.run(["kubectl", "get", "serviceaccount", k["service_account"], "-n", k["namespace"], "-o", "json"],
                        timeout=60, extra_env=self.kenv)
        if r.rc != 0:
            raise StepError(f"service account {k['namespace']}/{k['service_account']} not found: run Set up")
        ann = ((r.json({}) or {}).get("metadata") or {}).get("annotations") or {}
        if ann.get("JFrogExchange") != "true":
            raise StepError(f"{k['service_account']} lacks the annotation JFrogExchange=\"true\": the provider will not exchange its token")
        if platforms.kind(self.cfg) == "gke":
            if ann.get("iam.gke.io/gcp-service-account") != platforms.gsa_email(self.cfg):
                raise StepError(f"{k['service_account']} lacks iam.gke.io/gcp-service-account={platforms.gsa_email(self.cfg)}: run Set up")
            return "ok", "JFrogExchange: true; Google service account set"
        return "ok", "JFrogExchange: true"

    def s_trust(self):
        skip = self._need_token("the provider")
        if skip:
            return skip
        name, issuer = self.cfg["values"]["provider_name"], self.cfg["cluster"]["oidc_issuer"]
        have = self._provider_state().get(name)
        if not have:
            raise StepError(f"JFrog has no OIDC provider {name}: run Set up")
        got = (have.get("issuer_url") or "").rstrip("/")
        if got != (issuer or "").rstrip("/"):
            raise StepError(f"{name} trusts another issuer, not this cluster's")
        return "ok", f"{name} trusts this cluster"

    def s_map(self):
        skip = self._need_token("the mapping")
        if skip:
            return skip
        cfg = self.cfg
        prov, user, sub = cfg["values"]["provider_name"], cfg["jfrog"]["mapped_user"], model.subject(cfg)
        r = self.jf.mappings(prov)
        if not r["ok"]:
            raise StepError(f"identity mappings of {prov}: HTTP {r['status']}")
        for m in r["json"] or []:
            claims, spec = m.get("claims") or {}, m.get("token_spec") or {}
            if claims.get("sub") == sub:
                if spec.get("username") != user:
                    raise StepError(f"the mapping signs in as another user, not {user}")
                ttl = spec.get("expires_in")
                return "ok", f"{self.cfg['k8s']['service_account']} → {user}{f', {ttl // 60} min tokens' if isinstance(ttl, int) else ''}"
        raise StepError(f"no identity mapping for {self.cfg['k8s']['service_account']}: run Set up")

    def _image(self):
        """host/<repo>/<path>:<tag> → (repo, path, tag); tag is None for a digest reference."""
        ref = self.cfg["jfrog"]["test_image"]
        rest = ref.split("/", 1)[1] if "/" in ref else ref
        if "@" in rest:
            return rest.split("/", 1)[0], rest.split("/", 1)[1].split("@")[0], None
        name, _, tag = rest.rpartition(":")
        repo, _, path = name.partition("/")
        return repo, path, tag or "latest"

    def s_anon(self):
        repo, path, tag = self._image()
        host = self.cfg["jfrog"]["test_image"].split("/", 1)[0]
        r = self.jf._call("GET", f"/v2/{repo}/{path}/manifests/{tag or 'latest'}", host=host, auth=False)
        if r["status"] == 401:
            return "ok", "401 without credentials"
        if r["ok"]:
            return "warn", "anonymous access is on: a pull proves nothing"
        return "warn", f"HTTP {r['status'] or r['error']} without credentials"

    def s_who(self):
        if self.dry:
            return "skipped", "dry run: read after a real pull"
        skip = self._need_token("the download stats")
        if skip:
            return skip
        repo, path, tag = self._image()
        user = self.cfg["jfrog"]["mapped_user"]
        if tag is None:
            return "skipped", "the test image is a digest; JFrog's stats are read by tag"
        best = None
        for key in (repo, f"{repo}-cache"):
            for f in ("list.manifest.json", "manifest.json"):
                r = self.jf._call("GET", f"/artifactory/api/storage/{key}/{path}/{tag}/{f}?stats")
                d = r["json"] if r["ok"] and isinstance(r["json"], dict) else None
                if d and d.get("lastDownloaded") and (not best or d["lastDownloaded"] > best["lastDownloaded"]):
                    best = d
        if not best:
            raise StepError("JFrog has no download record for the test image")
        by, at = best.get("lastDownloadedBy", ""), best["lastDownloaded"] / 1000
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))
        if by != user:
            raise StepError(f"last downloaded by another user, not {user}")
        # A node that already holds the layers only re-checks the manifest (still with the exchanged
        # token: anonymous gets 401), and JFrog counts downloads, not checks. The stats' clock is JFrog's.
        started = self.facts.get("pull_started")
        if started and at >= started - 120:
            return "ok", f"downloaded by {user} at {when}"
        return "ok", f"downloaded by {user} at {when} (image cached on the node)"

    def s_uninstall(self):
        k = self.cfg["k8s"]
        self._run(["helm", "uninstall", k["release"], "--namespace", k["namespace"], *(["--dry-run"] if self.dry else [])], ok_rcs=(0, 1))
        return ("ok" if self.dry else "done"), "release removed" if not self.dry else "dry run"

    def s_unlabel(self):
        lab = self.cfg["values"]["node_affinity_label"]
        self._run(["kubectl", "label", "nodes", "--all", f"{lab}-", *(["--dry-run=server"] if self.dry else [])])
        return ("ok" if self.dry else "done"), "label removed"

    def s_delete_mapping(self):
        if self.dry:
            return "skipped", "dry run"
        r = self.jf.delete_mapping(self.cfg["values"]["provider_name"], self.cfg["jfrog"]["mapping_name"])
        return ("done" if r["ok"] or r["status"] == 404 else "failed"), f"HTTP {r['status']}"

    def s_gone(self):
        """Evidence for a tear down: the release, the DaemonSet, the node label and the provider are all absent."""
        if self.dry:
            return "skipped", "dry run: checked after a real tear down"
        k, lab = self.cfg["k8s"], self.cfg["values"]["node_affinity_label"]
        left = []
        if self.sh.run(["helm", "status", k["release"], "--namespace", k["namespace"]], timeout=60, extra_env=self.kenv).rc == 0:
            left.append("Helm release")
        for _ in range(12):  # the DaemonSet's pods go away after the release
            d = self._run(["kubectl", "get", "daemonset", "-n", k["namespace"], "-o", "json"], ok_rcs=(0, 1)).json({}) or {}
            if not [x for x in d.get("items", []) if "jfrog" in x["metadata"]["name"]]:
                break
            self.sleep(5)
        else:
            left.append("DaemonSet")
        n = self._run(["kubectl", "get", "nodes", "-l", f"{lab}=true", "-o", "json"]).json({}) or {}
        if n.get("items"):
            left.append(f"node label on {len(n['items'])} node(s)")
        if not self._jfrog_error():
            if self.cfg["values"]["provider_name"] in self._provider_state():
                left.append("JFrog OIDC provider")
        if left:
            raise StepError("still present: " + ", ".join(left))
        return "ok", "release, DaemonSet, node label and JFrog provider are gone"

    def s_delete_provider(self):
        name = self.cfg["values"]["provider_name"]
        if name in self.cfg["jfrog"]["protected_providers"]:
            raise StepError(f"{name} is protected")
        if self.dry:
            return "skipped", "dry run"
        r = self.jf.delete_provider(name)
        return ("done" if r["ok"] or r["status"] == 404 else "failed"), f"HTTP {r['status']}"
