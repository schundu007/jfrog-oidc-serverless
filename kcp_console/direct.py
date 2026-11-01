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

Prove (the stakeholder demo) adds, around the same proof:
  no-secret   no pull secret on the service account, none in its namespace (names and types only)
  no-keys     the mapped user holds no long-lived JFrog token
  renew-N     the service account is recreated: a new UID, so kubelet holds no cached JFrog token for it
  break       the identity mapping is removed for a moment (kept to restore it)
  refused     the same pull is refused while the trust is off
  restore     the mapping is put back; it runs even when an earlier step failed
  repull      the same pull works again
"""

from __future__ import annotations

import calendar
import json
import re
import time
from pathlib import Path

from . import model, platforms, redact
from .jfrog import Jfrog

CHART_REPO = "https://charts.jfrog.io"
CHART = "jfrog-credential-provider"
STEPS = ("cluster", "oidc", "kubeconfig", "sa", "provider", "mapping", "label", "helm", "daemonset", "anon", "pull", "who")
VERIFY_STEPS = ("cluster", "kubeconfig", "sa-check", "trust", "map", "daemonset", "anon", "pull", "who")
PROOF_STEPS = ("cluster", "sa-check", "trust", "map", "anon", "pull", "who")
PROVE_STEPS = ("cluster", "kubeconfig", "no-secret", "no-keys", "sa-check", "trust", "map", "anon", "renew-1", "granted-1", "pull", "who",
               "break", "renew-2", "denied", "refused", "restore", "renew-3", "granted-2", "repull")
# Steps that run even after a failure or a stop: the trust is never left off.
ALWAYS_STEPS = ("restore",)
LONG_LIVED_SECONDS = 86400
TEARDOWN_STEPS = ("kubeconfig", "uninstall", "unlabel", "delete-mapping", "delete-provider", "gone")
# GKE adds the Google service account that the pod's service account acts as (GCP.md Step 2B).
GKE_STEPS = STEPS[:3] + ("gsa",) + STEPS[3:]


def step_ids(cfg: dict, which: str) -> tuple:
    if which == "setup":
        return GKE_STEPS if platforms.kind(cfg) == "gke" else STEPS
    return {"verify": VERIFY_STEPS, "prove": PROVE_STEPS, "teardown": TEARDOWN_STEPS}[which]


class StepError(RuntimeError):
    pass


def _cause(event: str) -> str:
    """The reason in a kubelet event, without image references or URLs."""
    if "oauth token" in event.lower() and "401" in event:
        return "registry token request got 401: cached or expired credentials"
    if "failed to authorize" in event.lower() and re.search(r"\b401\b", event):
        return "JFrog answered 401: no credential for this pod"
    for k in ("401 Unauthorized", "403 Forbidden", "404 Not Found", "not found", "no basic auth credentials", "timeout"):
        if k.lower() in event.lower():
            return k
    return "see the cluster's events"


_URL = re.compile(r"https?://([^/\s\"]+)[^\s\"]*?(?=:?(?:\s|\"|$))")


def event_rows(items: list, uid: str = "") -> list:
    """The test pod's own events, oldest first, as the cluster wrote them: URLs cut to their host."""
    out = []
    for e in items:
        io = e.get("involvedObject") or {}
        if uid and io.get("uid") not in (None, uid):
            continue
        src = e.get("source") or {}
        out.append({"at": e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp") or "",
                    "type": e.get("type", ""), "reason": e.get("reason", ""),
                    "from": " on ".join(x for x in (src.get("component") or e.get("reportingComponent") or "",
                                                     src.get("host") or e.get("reportingInstance") or "") if x),
                    "count": e.get("count") or 1, "message": _URL.sub(r"\1", e.get("message", ""))[:600]})
    return sorted(out, key=lambda r: r["at"])


PLUGIN_LOG = "/logs/jfrog-credentials-provider/jfrog-credentials-provider.log"
# Setup chatter in every plugin run; the rest is what the plugin did and what JFrog said.
_PLUGIN_NOISE = re.compile(r"Fetching environment variable|getting envs - artifactoryUrl|cloud_provider from env|azureAuthMethod not set|Auto-update|request\.Image")
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")


def _epoch(iso: str) -> float:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", iso or "")
    if not m:
        return 0.0
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) + float((m.group(2) or ".0")[:7])


def plugin_rows(text: str, since: float, until: float = 0.0, limit: int = 40) -> list:
    """The credential plugin's own log lines (JSON per line, on the node) for one pull: time, level and
    message, setup chatter dropped, URLs cut to host and path, never a token."""
    rows = []
    for line in (text or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = _epoch(e.get("time", ""))
        if not isinstance(e, dict) or t < since or (until and t > until) or _PLUGIN_NOISE.search(e.get("msg", "")):
            continue
        msg = re.sub(r"https?://([^\s?\"]+)(\?\S*)?", r"\1", _JWT.sub("[token]", str(e.get("msg", ""))))
        rows.append({"at": e["time"][:19] + "Z", "level": e.get("level", ""), "message": redact.text(msg)[:600]})
    return rows[-limit:]


def exchange_answer(res: dict) -> str:
    """JFrog's words from a token exchange reply, never the token: on success who it is for and for how
    long, else JFrog's error messages."""
    js = res.get("json") if isinstance(res.get("json"), dict) else {}
    if res.get("status") == 200:
        who = js.get("username") or (js.get("scope") or "").replace("applied-permissions/user", "").strip() or "the mapped user"
        return f"token issued for {who}" + (f", expires in {js['expires_in'] // 60} min" if isinstance(js.get("expires_in"), int) else "")
    errs = [e.get("message", "") for e in js.get("errors") or [] if isinstance(e, dict)]
    text = "; ".join(m for m in errs if m) or js.get("error_description") or js.get("message") or str(res.get("body") or res.get("error") or "")
    return redact.text(re.sub(r"\s+", " ", str(text)))[:300]


def _cluster_name(cfg: dict) -> str:
    c = cfg.get("cluster")
    return (c.get("name") if isinstance(c, dict) else c) or cfg.get("name", "")


def _why(msgs: list) -> str:
    """The cause in the kubelet's 'Failed to pull' event, not the ErrImagePull/BackOff lines after it."""
    full = [m for m in msgs if m.startswith("Failed to pull")]
    return _cause((full or msgs)[-1])


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
        "no-secret": ("No pull secret on the service account or in its namespace", "read",
                      ["kubectl", *kc, "get", "serviceaccount", k["service_account"], "-n", k["namespace"], "-o", "json"]),
        "no-keys": (f"{j['mapped_user']} holds no long-lived JFrog token", "read", f"GET https://{host}/access/api/v1/tokens"),
        "renew-1": ("Fresh service account: no cached token", "change", RENEW(kc, k, p)),
        "break": (f"Remove identity mapping {j['mapping_name']}", "change",
                  f"DELETE https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings/{j['mapping_name']}"),
        "renew-2": ("Fresh service account without the trust", "change", RENEW(kc, k, p)),
        "denied": ("JFrog refuses the exchange without the trust", "read", f"POST https://{host}/access/api/v1/oidc/token  (as {k['service_account']})"),
        "granted-1": ("JFrog grants the exchange", "read", f"POST https://{host}/access/api/v1/oidc/token  (as {k['service_account']})"),
        "granted-2": ("JFrog grants the exchange again", "read", f"POST https://{host}/access/api/v1/oidc/token  (as {k['service_account']})"),
        "refused": ("Pull refused without the trust", "change", ["kubectl", *kc, "apply", "-f", str(p["pod"]), *srv]),
        "restore": (f"Restore identity mapping {j['mapping_name']}", "change",
                    f"POST https://{host}/access/api/v1/oidc/{v['provider_name']}/identity_mappings  (the mapping as it was)"),
        "renew-3": ("Fresh service account with the trust back", "change", RENEW(kc, k, p)),
        "repull": ("Pull works again with the trust", "change", ["kubectl", *kc, "apply", "-f", str(p["pod"]), *srv]),
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
        skipped_in_dry = dry_run and i in ("oidc", "gsa", "provider", "mapping", "delete-mapping", "delete-provider", "break", "restore")
        out.append({"id": i, "title": title, "kind": kind, "command": cmd if isinstance(cmd, str) else " ".join(cmd),
                    "dry_run_note": "shown, not sent: this call has no dry run" if skipped_in_dry else ""})
    return out


def RENEW(kc, k, p) -> list:
    return ["kubectl", *kc, "delete", "serviceaccount", k["service_account"], "-n", k["namespace"], "&&",
            "kubectl", *kc, "apply", "-f", str(p["sa"])]


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

    def _run(self, argv, timeout=300, ok_rcs=(0,), stream=False):
        r = self.sh.run(argv, timeout=timeout, extra_env=self.kenv, stream=stream)
        if r.rc not in ok_rcs:
            raise StepError(redact.text((r.stderr or r.stdout or f"exit {r.rc}").strip().splitlines()[-1] if (r.stderr or r.stdout) else f"exit {r.rc}"))
        return r

    def execute(self, steps: list, stop=lambda: False) -> list:
        results, ended = [], False
        for st in steps:
            if not ended and stop():
                self.log("■ stopped before " + st["title"])
                ended = True
            if ended and st["id"] not in ALWAYS_STEPS:
                continue
            fn = getattr(self, "s_" + re.sub(r"-\d+$", "", st["id"]).replace("-", "_"))
            self.log(f"▶ {st['title']}")
            try:
                res = fn()
                status, detail, *more = res if isinstance(res, tuple) else ("ok", res)
            except StepError as exc:
                results.append({"id": st["id"], "title": st["title"], "status": "failed", "detail": str(exc)})
                self.log(f"✗ {st['title']}: {exc}")
                ended = True
                continue
            row = {"id": st["id"], "title": st["title"], "status": status, "detail": detail}
            if more:
                row["facts"] = more[0]
            results.append(row)
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
        self._run(platforms.enable(self.cfg), timeout=1800, stream=True)
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
                   *(["--dry-run=server"] if self.dry else [])], timeout=600, stream=not self.dry)
        if self.dry:
            return "ok", f"{CHART} {v['chart_version']} rendered and validated by the API server"
        # An upgrade with the same pod template leaves the old pods in place, so the nodes keep the old
        # provider config and kubelet keeps its cached tokens. Restart them, as the runbook's install does.
        d = self._run(["kubectl", "get", "daemonset", "-n", k["namespace"], "-o", "json"]).json({}) or {}
        names = [x["metadata"]["name"] for x in d.get("items", []) if "jfrog" in x["metadata"]["name"]]
        for n in names:
            self._run(["kubectl", "rollout", "restart", f"daemonset/{n}", "-n", k["namespace"]])
            self._run(["kubectl", "rollout", "status", f"daemonset/{n}", "-n", k["namespace"], "--timeout=300s"], timeout=320, stream=True)
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
        if self.dry:
            return self._dry_pod()
        ok, reason, msgs, secs = self._pull()
        if ok:
            return "done", "pulled with no pull secret", {"pulled": True, "seconds": secs, "events": self._events, "pod": self._pod}
        raise StepError(f"pull failed ({reason}){': ' + _why(msgs) if msgs else ''}")

    def s_repull(self):
        if self.dry:
            return self._dry_pod()
        ok, reason, msgs, secs = self._pull()
        if ok:
            return "done", f"pulled again in {secs} s", {"pulled": True, "seconds": secs, "events": self._events, "pod": self._pod}
        raise StepError(f"pull failed after the trust came back ({reason}){': ' + _why(msgs) if msgs else ''}")

    def s_refused(self):
        if self.dry:
            return self._dry_pod()
        ok, reason, msgs, secs = self._pull()
        if ok:
            raise StepError("pulled without the trust: the pull does not depend on OIDC")
        why = _why(msgs) if msgs else reason
        return "ok", f"refused ({reason}: {why})", {"pulled": False, "reason": reason, "cause": why,
                                                     "events": self._events, "pod": self._pod}

    def _dry_pod(self):
        self.p["pod"].write_text(json.dumps(pod_manifest(self.cfg), indent=2))
        flag = self._dry_flag()
        self._run(["kubectl", "apply", "-f", str(self.p["pod"]), flag])
        return "skipped", f"dry run: pod validated {'by the API server' if flag.endswith('server') else 'client-side'}"

    def _pull(self):
        """(pulled, reason, kubelet event messages, seconds) for one test pod with no pull secret."""
        k = self.cfg["k8s"]
        self.p["pod"].write_text(json.dumps(pod_manifest(self.cfg), indent=2))
        self._run(["kubectl", "delete", "pod", k["test_pod"], "-n", k["namespace"], "--ignore-not-found", "--wait=true"], timeout=120)
        self.facts["pull_started"] = self.clock()
        self._run(["kubectl", "apply", "-f", str(self.p["pod"])])
        waited, phase, reason, uid, node = 0, "", "", "", ""
        while waited < self.pull_timeout:
            d = self._run(["kubectl", "get", "pod", k["test_pod"], "-n", k["namespace"], "-o", "json"]).json({}) or {}
            uid = (d.get("metadata") or {}).get("uid", uid)
            node = (d.get("spec") or {}).get("nodeName") or node
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
        self._events = event_rows(ev.get("items", []), uid)
        # Where the events were read from, so the evidence names its own source.
        self._pod = {"platform": platforms.kind(self.cfg), "cluster": _cluster_name(self.cfg),
                     "namespace": k["namespace"], "pod": k["test_pod"], "uid": uid,
                     "node": node or next((e["from"].split(" on ", 1)[1] for e in self._events if " on " in e["from"]), ""),
                     "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self._pod["plugin_log"] = self._plugin_log(self._pod["node"])
        self._run(["kubectl", "delete", "pod", k["test_pod"], "-n", k["namespace"], "--ignore-not-found", "--wait=false"])
        secs = int(self.clock() - self.facts["pull_started"]) if isinstance(self.facts.get("pull_started"), (int, float)) else waited
        return phase in ("Running", "Succeeded"), reason or phase or "timeout", msgs, max(secs, 0)

    def _plugin_log(self, node: str):
        """The plugin's log on the node that pulled, read through the API server; None when unreadable."""
        if not node:
            return None
        r = self.sh.run(["kubectl", "get", "--raw", f"/api/v1/nodes/{node}/proxy{PLUGIN_LOG}"], timeout=90, extra_env=self.kenv)
        if r.rc != 0:
            return None
        start = self.facts.get("pull_started")
        return plugin_rows(r.stdout, (start - 2) if isinstance(start, (int, float)) else 0.0)

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

    # -- Prove: no secret, and no pull without the trust ------------------------------

    def s_no_secret(self):
        k = self.cfg["k8s"]
        r = self._run(["kubectl", "get", "serviceaccount", k["service_account"], "-n", k["namespace"], "-o", "json"])
        on_sa = len((r.json({}) or {}).get("imagePullSecrets") or [])
        # Names and types only: the secrets' data never leaves the cluster.
        r = self._run(["kubectl", "get", "secrets", "-n", k["namespace"], "-o",
                       "jsonpath={range .items[*]}{.type}{\"\\n\"}{end}"])
        in_ns = sum(1 for t in r.stdout.splitlines() if t.strip() in ("kubernetes.io/dockerconfigjson", "kubernetes.io/dockercfg"))
        facts = {"pull_secrets": on_sa + in_ns, "on_service_account": on_sa, "in_namespace": in_ns}
        if on_sa or in_ns:
            raise StepError(f"{on_sa} pull secret(s) on the service account, {in_ns} registry secret(s) in {k['namespace']}: "
                            "a pull could use them, so it proves nothing")
        return "ok", f"0 on the service account, 0 registry secrets in {k['namespace']}; the test pod sets imagePullSecrets: []", facts

    def s_no_keys(self):
        skip = self._need_token("the token list")
        if skip:
            return skip
        user = self.cfg["jfrog"]["mapped_user"]
        r = self.jf._call("GET", "/access/api/v1/tokens")
        if not r["ok"]:
            return "warn", f"JFrog's token list was not readable (HTTP {r['status'] or r['error']})"
        toks = (r["json"] or {}).get("tokens") or [] if isinstance(r["json"], dict) else []
        mine = [t for t in toks if str(t.get("subject", "")).rstrip("/").split("/")[-1] == user]
        long_lived = [t for t in mine if not t.get("expiry") or t["expiry"] - t.get("issued_at", t["expiry"]) > LONG_LIVED_SECONDS]
        facts = {"long_lived": len(long_lived), "short_lived": len(mine) - len(long_lived)}
        if long_lived:
            raise StepError(f"{user} holds {len(long_lived)} long-lived token(s): a stored token could pull instead")
        return "ok", f"0 long-lived; {facts['short_lived']} short-lived from the exchange", facts

    def s_renew(self):
        """Recreate the service account: kubelet caches the plugin's result per service account and UID,
        so a new UID makes the next pull exchange a fresh token."""
        k = self.cfg["k8s"]
        self.p["sa"].write_text(json.dumps(sa_manifest(self.cfg), indent=2))
        if self.dry:
            return "skipped", "dry run: would recreate the service account"
        self._run(["kubectl", "delete", "serviceaccount", k["service_account"], "-n", k["namespace"], "--ignore-not-found", "--wait=true"])
        self._run(["kubectl", "apply", "-f", str(self.p["sa"])])
        return "done", "recreated with a new UID: no cached credential"

    def _ask_jfrog(self):
        """Ask JFrog's token endpoint with the pod service account's own token, as the credential provider
        does. Returns JFrog's answer without any token: HTTP status, its message or who the token is for."""
        k, v = self.cfg["k8s"], self.cfg["values"]
        aud = v["gcp_oidc_audience"] if platforms.kind(self.cfg) == "gke" else v["azure_app_audience"]
        r = self.sh.run(["kubectl", "create", "token", k["service_account"], "-n", k["namespace"], "--audience", aud,
                         "--duration", "10m"], timeout=60, extra_env=self.kenv)
        if r.rc != 0:
            raise StepError(f"no service account token for {k['service_account']}")
        res = self.jf.exchange(v["provider_name"], r.stdout.strip())
        return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "endpoint": "/access/api/v1/oidc/token",
                "provider": v["provider_name"], "subject": model.subject(self.cfg), "audience": aud,
                "status": res["status"], "answer": exchange_answer(res)}

    def s_granted(self):
        if self.dry:
            return "skipped", "dry run: JFrog was not asked"
        a = self._ask_jfrog()
        if a["status"] != 200:
            raise StepError(f"JFrog refused the exchange: HTTP {a['status']}: {a['answer']}")
        return "ok", f"HTTP 200: {a['answer']}", {"jfrog": a}

    def s_denied(self):
        if self.dry:
            return "skipped", "dry run: JFrog was not asked"
        a = self._ask_jfrog()
        if a["status"] == 200:
            raise StepError("JFrog granted the exchange without the mapping: the trust is not what decides")
        return "ok", f"HTTP {a['status']}: {a['answer']}", {"jfrog": a}

    def _saved(self):
        return self.p["dir"] / f"{self.cfg['name']}-mapping-saved.json"

    def s_break(self):
        skip = self._need_token("the mapping")
        if skip:
            return skip
        prov, sub = self.cfg["values"]["provider_name"], model.subject(self.cfg)
        r = self.jf.mappings(prov)
        if not r["ok"]:
            raise StepError(f"identity mappings of {prov}: HTTP {r['status']}")
        m = next((m for m in r["json"] or [] if (m.get("claims") or {}).get("sub") == sub), None)
        if not m:
            raise StepError(f"no identity mapping for {self.cfg['k8s']['service_account']}: run Set up")
        if self.dry:
            return "skipped", f"dry run: would remove {m.get('name')} for a moment"
        self._saved().write_text(json.dumps(m))  # survives a console restart; Set up recreates it too
        self.facts["saved_mapping"] = m
        d = self.jf.delete_mapping(prov, m["name"])
        if not d["ok"]:
            raise StepError(f"remove mapping: HTTP {d['status']}")
        self.facts["broken_at"] = self.clock()
        return "done", f"{m['name']} removed: JFrog no longer trusts {self.cfg['k8s']['service_account']}"

    def s_restore(self):
        m = self.facts.get("saved_mapping")
        if not m and self._saved().exists():
            m = json.loads(self._saved().read_text())
        if not m:
            return "skipped", "nothing was removed"
        if self.dry:
            return "skipped", "dry run"
        body = {key: m[key] for key in ("name", "description", "claims", "token_spec", "priority") if key in m}
        r = self.jf.create_mapping(self.cfg["values"]["provider_name"], body)
        if not r["ok"] and r["status"] != 409:
            raise StepError(f"restore mapping: HTTP {r['status']}: run Set up to recreate it")
        self._saved().unlink(missing_ok=True)
        off = int(self.clock() - self.facts["broken_at"]) if "broken_at" in self.facts else None
        return "done", f"{body['name']} restored" + (f" after {off} s" if off is not None else ""), {"trust_off_seconds": off}

    def s_uninstall(self):
        k = self.cfg["k8s"]
        self._run(["helm", "uninstall", k["release"], "--namespace", k["namespace"], *(["--dry-run"] if self.dry else [])], ok_rcs=(0, 1), stream=True)
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
