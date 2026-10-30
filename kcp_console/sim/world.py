"""The simulated world: AKS nodes, kubelet, the plugin, containerd, the DaemonSet
setup step, orphaned writers and Artifactory, on a virtual clock (spec §8).

Behaviour, not binaries. Every decision goes through kcp_console.logic (token
URL, MergeFiles, matchImages, reset safety), and every output is rendered in
the formats the real pipeline prints (pod events, the QUICK SUMMARY footer, the
plugin's JSON lines, helm values), so the engine hands them to the console's
own parsers, checks and rule engine.

Modelling choices:
- kubelet reads the provider config once, at its own start.
- The setup step writes the release's entry with MergeFiles' observed
  behaviour (a changed artifactory_url spelling keeps the stale entry) and
  restarts kubelet after a write, as seen on sandbox. `setup_restarts_kubelet`
  turns the restart off to model "fixed on disk, not loaded".
- An orphaned writer rewrites its own stale entry one second after every
  setup write on the nodes it ran on, with a kubelet restart. Deleting its
  DaemonSet does not stop it on those nodes (sandbox: it stopped only when the
  node was replaced).
- kubelet caches the plugin's credential per (service account, registry) for
  defaultCacheDuration; the node caches pulled images.
- A pod whose service account lacks the JFrogExchange annotation gets no
  projected token, so the plugin fails and containerd sees no credential.
"""

from __future__ import annotations

import copy
import datetime as dt
import json

from .. import logic, model
from ..parse import kubelet_config

T0 = dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
UPSTREAM_BINARY = 14_359_603
IMAGE_BYTES = 142_944_877
PULL_SECONDS = 10.487


def iso(t: float) -> str:
    return (T0 + dt.timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Node:
    def __init__(self, name, t, labelled=True, pool="appnodepool"):
        self.name, self.created, self.labelled, self.pool = name, t, labelled, pool
        self.config = [copy.deepcopy(kubelet_config.ACR_ENTRY)]
        self.mtime = t
        self.kubelet_started = t + 1
        self.loaded = copy.deepcopy(self.config)
        self.images = set()
        self.token_cache = {}          # (sa, registry host) -> (expiry t, username)
        self.plugin_log = []           # rendered JSON lines
        self.journal = []
        self.binary_bytes = None
        self.backup = None


class Artifactory:
    def __init__(self, cfg):
        j = cfg["jfrog"]
        self.host = j["host"]
        self.providers = {}
        self.mappings = {}
        self.repos = {
            j["test_repo"]: {"type": "local", "images": set()},
            j["promote_source_repo"]: {"type": "local", "images": {"mcp/sonarqube:1.26-v1", "mcp/sonarqube:1.27-v1"}},
            "docker-virtual": {"type": "virtual", "images": {"mcp/sonarqube:1.26-v1"}},
            j["binary_repo"]: {"type": "local", "images": set()},
            "example-docker-remote": {"type": "remote", "images": set(), "dormant": True},
        }
        self.permissions = {j["mapped_user"]: {j["test_repo"]}}
        self.files = {}
        self.audit = []

    def repo_from_host(self, host):
        server, _, domain = self.host.partition(".")
        if host == self.host:
            return None
        if host.startswith(server + "-") and host.endswith("." + domain):
            return host[len(server) + 1: -(len(domain) + 1)]
        return "?"

    def exchange(self, t, provider, claims):
        p = self.providers.get(provider)
        rec = {"t": iso(t), "service": "access", "method": "POST", "path": "/access/api/v1/oidc/token",
               "subject": claims["sub"], "provider": provider}
        if not p:
            self.audit.append(dict(rec, status=401, user=""))
            return 401, f"OIDC provider {provider} not found"
        if p["issuer_url"].rstrip("/") != claims["iss"].rstrip("/"):
            self.audit.append(dict(rec, status=401, user=""))
            return 401, "token issuer does not match the provider's issuer_url"
        for m in self.mappings.get(provider, []):
            if m["claims"].get("sub") == claims["sub"]:
                user = m["token_spec"]["username"]
                self.audit.append(dict(rec, status=200, user=user))
                return 200, user
        self.audit.append(dict(rec, status=401, user=""))
        return 401, "no identity mapping matches the token's claims"

    def pull(self, t, host, path, user):
        """Registry v2 manifest GET. path is image-path:tag."""
        repo = self.repo_from_host(host)
        if repo is None:
            return 400, "Unsupported v2 repository request for ''"
        r = self.repos.get(repo)
        if not r:
            return 404, f"repository {repo} not found"
        if user is None:
            return 401, "anonymous"
        rec = {"t": iso(t), "service": "artifactory", "method": "GET",
               "path": f"/api/docker/{repo}/v2/{path.replace(':', '/manifests/')}", "user": user}
        if repo not in self.permissions.get(user, set()):
            self.audit.append(dict(rec, status=403))
            return 403, f"denied: user {user} has no read permission on {repo}"
        if path not in r["images"]:
            return 404, "manifest unknown"
        self.audit.append(dict(rec, status=200))
        return 200, "ok"


class World:
    def __init__(self, cfg=None, *, nodes=None, system_node=True):
        self.cfg = model.merged(cfg or {})
        self.t = 0.0
        self.events, self.chain, self.evidence = [], [], {}
        self.art = Artifactory(self.cfg)
        self.nodes = {}
        if system_node:
            n = Node("aks-systempool-11223344-vmss000000", 0, labelled=False, pool="systempool")
            self.nodes[n.name] = n
        for name in nodes or self.cfg["cluster"]["nodes"]:
            self.nodes[name] = Node(name, 0, labelled=False)
        self.sa_annotated = {self.cfg["k8s"]["service_account"]: True, "default": False}
        self.release = None
        self.ds_ready = False
        self.ds_pods_config = None       # the values the running provider pods were started with
        self.orphans = []
        self.setup_restarts_kubelet = True
        self.pods = {}
        self.kubelet_checks = []
        self.install_logs, self.reset_logs = [], []
        self.issuer = self.cfg["cluster"]["oidc_issuer"]

    # ------------------------------------------------------------ plumbing

    def ev(self, lane, kind, detail, node=None, ok=None, **extra):
        e = {"t": round(self.t, 3), "time": iso(self.t), "lane": lane, "kind": kind, "detail": detail}
        if node:
            e["node"] = node
        if ok is not None:
            e["ok"] = ok
        e.update(extra)
        self.events.append(e)
        return e

    def because(self, text):
        self.chain.append(f"{iso(self.t)} {text}")

    def advance(self, seconds):
        self.t += float(seconds)
        self.ev("pipeline", "wait", f"{logic.human_duration(seconds)} pass")

    def labelled_nodes(self):
        return [n for n in sorted(self.nodes.values(), key=lambda n: n.name) if n.labelled]

    def values(self):
        return self.cfg["values"]

    def release_entry(self, v):
        return kubelet_config.jfrog_entry(v["artifactory_url"], v["match_images"], v["provider_name"],
                                          v["default_cache_duration"], v["azure_app_audience"])

    # ------------------------------------------------------------ node file writers

    def write_node(self, node, providers, writer, restart=True):
        node.config = copy.deepcopy(providers)
        node.mtime = self.t
        j = next((p for p in providers if p["name"] == logic.JFROG_PROVIDER), None)
        url = logic.entry_url(j) if j else None
        self.ev("writes", "config-write", f"{writer} wrote the node file (artifactory_url={url})", node=node.name,
                writer=writer, ok=bool(url) and logic.is_bare_host(url))
        if restart:
            self._kubelet_restart(node)
        for o in self.orphans:
            if node.name in o["nodes"] and writer != o["name"]:
                self.t += 1
                merged, _ = logic.merge_files([p for p in node.config if p["name"] != logic.JFROG_PROVIDER], o["entry"])
                self.because(f"{o['namespace']}/{o['name']} (orphaned, {o['age_days']} days old) reverted "
                             f"{node.name}'s config within a second of the correct write")
                node.config = merged
                node.mtime = self.t
                self.ev("writes", "config-write", f"{o['namespace']}/{o['name']} reverted the node file",
                        node=node.name, writer=o["name"], ok=False)
                self._kubelet_restart(node)

    def _kubelet_restart(self, node):
        self.t += 1
        node.kubelet_started = self.t
        node.loaded = copy.deepcopy(node.config)
        node.token_cache.clear()
        self.ev("kubelet", "restart", "kubelet restarted and read the provider config", node=node.name)

    def setup_step(self, node):
        """The DaemonSet's setup container on one node: MergeFiles, then a kubelet restart."""
        node.binary_bytes = self.art.files.get(self._download_path(self.ds_pods_config["download_url"]))
        new, action = logic.merge_files(node.config, self.release_entry(self.ds_pods_config), observed=True)
        if action == "kept-stale":
            old = logic.entry_url(next(p for p in node.config if p["name"] == logic.JFROG_PROVIDER))
            self.because(f"setup step on {node.name}: the existing JFrog entry has artifactory_url={old}; MergeFiles "
                         f"replaces only an identical artifactory_url, so the stale entry stayed")
            self.ev("writes", "merge", "MergeFiles kept the stale entry (different artifactory_url)", node=node.name, ok=False)
            return
        self.write_node(node, new, "jfrog-cred-provider setup", restart=self.setup_restarts_kubelet)
        if not self.setup_restarts_kubelet:
            self.because(f"{node.name}: the config file was rewritten but kubelet was not restarted; it still runs "
                         f"the config it read at its own start")

    # ------------------------------------------------------------ Artifactory-side setup

    def create_provider(self, name=None, issuer=None):
        name = name or self.values()["provider_name"]
        self.art.providers[name] = {"name": name, "issuer_url": issuer or self.issuer, "provider_type": "Azure"}
        self.ev("artifactory", "provider", f"OIDC provider {name} created", ok=True)

    def create_mapping(self, provider=None, sub=None, user=None):
        provider = provider or self.values()["provider_name"]
        req = model.mapping_request(self.cfg)
        if sub:
            req["claims"]["sub"] = sub
        if user:
            req["token_spec"]["username"] = user
        self.art.mappings.setdefault(provider, []).append(req)
        self.ev("artifactory", "mapping", f"identity mapping {req['name']} ({req['claims']['sub']} → "
                                          f"{req['token_spec']['username']})", ok=True)

    def foreign_provider(self):
        self.art.providers["aks-other-cluster-oidc"] = {
            "name": "aks-other-cluster-oidc", "provider_type": "Azure",
            "issuer_url": "https://eastus2.oic.prod-aks.azure.com/00000000-0000-4000-8000-0000000000aa/"
                          "11111111-0000-4000-8000-0000000000bb/"}

    def grant(self, user, repo, on=True):
        perms = self.art.permissions.setdefault(user, set())
        (perms.add if on else perms.discard)(repo)

    # ------------------------------------------------------------ binary mirror

    def _download_path(self, url):
        return url.split("/artifactory/", 1)[-1]

    def mirror_binary(self, follow_redirects=True, size_guard=True):
        path = self._download_path(self.values()["download_url"])
        size = UPSTREAM_BINARY if follow_redirects else 0
        log = [f"curl {'-L ' if follow_redirects else ''}-fsS -o jfrog-credential-provider "
               f"https://releases.jfrog.io/artifactory/jfrog-credential-provider/...", f"downloaded {size} bytes"]
        if not follow_redirects:
            self.because("curl without -L saved the empty body of releases.jfrog.io's 302 and reported success")
        if size_guard and size < logic.MIN_BINARY_BYTES:
            log.append(f"ERROR: refusing to mirror a {size}-byte binary (under 1 MB)")
            self.ev("pipeline", "mirror", "the size guard refused the binary", ok=False)
            self.evidence["mirror"] = {"size": size, "uploaded": False, "log": "\n".join(log)}
            return False
        self.art.files[path] = size
        log.append(f"PUT {path} → 201")
        self.ev("artifactory", "mirror", f"binary mirrored ({size:,} bytes)", ok=size >= logic.MIN_BINARY_BYTES)
        self.evidence["mirror"] = {"size": size, "uploaded": True, "path": path, "log": "\n".join(log)}
        return True

    # ------------------------------------------------------------ install

    def install(self, wait=False, delete_pods=True, values=None):
        """INSTALL_JFROG_PROVIDER: label nodes, helm upgrade --install, delete old provider pods."""
        v = copy.deepcopy(self.values())
        v.update(values or {})          # a release built from other values than the committed file
        for n in self.nodes.values():
            if n.pool != "systempool":
                n.labelled = True
        self.release = {"revision": (self.release or {}).get("revision", 0) + 1, "values": v}
        log = [f"kubectl label nodes --all credentialsProviderEnabled=true --overwrite",
               f"helm upgrade --install {self.cfg['k8s']['release']} {v['chart_name']} --version {v['chart_version']} "
               f"-f {self.cfg['ado']['values_path']} --set binaryDownload.auth.accessToken=***" + (" --wait" if wait else "")]
        self.ev("pipeline", "install", f"helm upgrade --install (revision {self.release['revision']})")
        restarted = delete_pods or self.ds_pods_config is None
        if restarted:
            self.ds_pods_config = v
            log.append("deleted existing provider pods so they pick up the new config")
        else:
            log.append("provider pods left running: Kubernetes does not restart a pod when a ConfigMap it mounts changes")
        url = self.ds_pods_config["download_url"]
        stored = self.art.files.get(self._download_path(url))
        if any(url.endswith(s) for s in logic.ARCH_SUFFIXES):
            log.append(f"init container: curl: (22) The requested URL returned error: 401 ({url}-amd64)")
            self.because("downloadUrl already ended in an arch suffix and the chart appended -amd64 again, so the "
                         "init container got 401")
            self.ds_ready = False
        elif stored is None:
            log.append("init container: curl: (22) The requested URL returned error: 404")
            self.ds_ready = False
        else:
            self.ds_ready = True
        if wait and (not self.ds_ready or not restarted):
            log.append("Error: UPGRADE FAILED: timed out waiting for the condition (DaemonSet Available: 0/1)")
            self.because("helm --wait polled the same broken provider pod: a ConfigMap change does not restart it")
            self.ev("pipeline", "install", "helm --wait timed out", ok=False)
            self.install_logs.append("\n".join(log))
            return False
        if self.ds_ready and restarted:
            for n in self.labelled_nodes():
                self.t += 2
                self.setup_step(n)
            if stored is not None and stored < logic.MIN_BINARY_BYTES:
                self.because(f"the init container installed a {stored}-byte binary")
            log.append(f"daemonset {self.cfg['k8s']['daemonset']}: {len(self.labelled_nodes())} of "
                       f"{len(self.labelled_nodes())} available")
        self.ev("pipeline", "install", "install finished", ok=self.ds_ready)
        self.install_logs.append("\n".join(log))
        return self.ds_ready

    def restart_daemonset(self, why):
        self.ev("pipeline", "ds-restart", f"DaemonSet restarted ({why})")
        if self.ds_ready:
            for n in self.labelled_nodes():
                self.t += 1
                self.setup_step(n)

    def add_orphan(self, namespace="jfrog-provider", match_images=None, provider=None, url=None):
        v = self.values()
        entry = kubelet_config.jfrog_entry(url or v["artifactory_url"], match_images or ["example-jfrog-releases-remote.jfrog.io"],
                                           provider or "aks-other-cluster-oidc", v["default_cache_duration"])
        self.orphans.append({"namespace": namespace, "name": "jfrog-credential-provider",
                             "nodes": {n.name for n in self.nodes.values() if n.pool != "systempool"},
                             "entry": entry, "age_days": 78})

    def delete_orphans(self):
        for o in self.orphans:
            o["deleted"] = True
        self.ev("pipeline", "cleanup", "CLEANUP_STALE_JFROG_DAEMONSET deleted the orphaned DaemonSet")

    def replace_node(self, old, new):
        self.nodes.pop(old, None)
        for o in self.orphans:
            o["nodes"].discard(old)
        for p in self.pods.values():
            if p["node"] == old:
                p["node"] = None
        self.nodes[new] = Node(new, self.t, labelled=True)
        self.ev("kubelet", "node", f"node {old} replaced by {new}", node=new)
        self.because(f"node {old} replaced by {new}")
        if self.ds_ready:
            self.t += 5
            self.setup_step(self.nodes[new])

    def add_node(self, name):
        self.nodes[name] = Node(name, self.t, labelled=True)
        self.ev("kubelet", "node", f"new node {name}", node=name)
        self.because(f"new node {name} joined; the DaemonSet writes its entry for the first time")
        if self.ds_ready:
            self.t += 5
            self.setup_step(self.nodes[name])

    # ------------------------------------------------------------ reset

    def reset_node_entries(self, dry_run=True, fixed=True):
        lines = [f"RESET_NODE_JFROG_ENTRY DryRun={str(dry_run).lower()}"]
        for n in self.labelled_nodes():
            plan = logic.reset_plan(kubelet_config.render(n.config), fixed=fixed)
            if plan["action"] == "skip":
                lines.append(f"[{n.name}] {plan['reason']}")
            elif plan["action"] == "abort":
                lines.append(f"[{n.name}] ABORT: another provider entry follows ({', '.join(plan['following'][:3])})")
                if not fixed:
                    self.because(f"reset on {n.name} aborted: the safety check counted the JFrog entry's own env: "
                                 f"items as provider entries")
            elif dry_run:
                lines.append(f"[{n.name}] would back up to {plan['backup']} and remove the JFrog entry "
                             f"(artifactory_url={', '.join(plan['urls'])})")
            else:
                n.backup = copy.deepcopy(n.config)
                lines.append(f"[{n.name}] backed up to {plan['backup']}; removed the JFrog entry")
                lines.append(plan["text"])
                self.write_node(n, kubelet_config.parse(plan["text"])["providers"], "RESET_NODE_JFROG_ENTRY", restart=False)
                self.because(f"reset removed {n.name}'s stale JFrog entry")
        self.reset_logs.append("\n".join(lines))
        if not dry_run:
            self.restart_daemonset("reset")

    # ------------------------------------------------------------ pods

    def create_pod(self, name=None, image=None, sa=None, node=None, kind="test"):
        """CREATE_TEST_POD: delete, recreate, poll up to 2 minutes, print events."""
        name = name or self.cfg["k8s"]["test_pod"]
        image = image or self.cfg["jfrog"]["test_image"]
        sa = sa or self.cfg["k8s"]["service_account"]
        n = self.nodes[node] if node else (self.labelled_nodes() or sorted(self.nodes.values(), key=lambda x: x.name))[0]
        host = logic.image_host(image)
        path = image.split("/", 1)[1]
        ns = self.cfg["k8s"]["namespace"]
        ev = [f"Normal  Scheduled  default-scheduler  Successfully assigned {ns}/{name} to {n.name}"]
        self.t += 1
        ok = False
        if f"{host}|{path}" in n.images:
            ev.append(f'Normal  Pulled  kubelet  Container image "{image}" already present on machine')
            ok = True
            self.ev("image-cache", "hit", f"{image} already on {n.name}", node=n.name, ok=True)
            self.because(f"{name}: the image was already present on {n.name}; no registry call was made")
        else:
            ev.append(f'Normal  Pulling  kubelet  Pulling image "{image}"')
            cred = self._credential(n, image, sa, name)
            status, msg = self.art.pull(self.t, host, path, cred)
            self.ev("artifactory", "registry", f"GET manifest {host}/{path} as {cred or 'anonymous'} → {status}", ok=status == 200)
            if status == 200:
                self.t += PULL_SECONDS
                ev.append(f'Normal  Pulled  kubelet  Successfully pulled image "{image}" in {PULL_SECONDS}s '
                          f'({PULL_SECONDS}s including waiting). Image size: {IMAGE_BYTES} bytes.')
                n.images.add(f"{host}|{path}")
                ok = True
                self.ev("image-cache", "store", f"{image} cached on {n.name}", node=n.name)
            else:
                repo = self.art.repo_from_host(host)
                if status == 400:
                    detail = f"unexpected status: 400 Bad Request: {msg}"
                    self.because(f"{name}: registry request against the bare domain: {msg}")
                elif status == 403:
                    detail = f"failed to resolve reference: unexpected status from HEAD request: 403 Forbidden ({msg})"
                    self.because(f"{name}: the token was issued but {msg}")
                elif status == 404:
                    detail = f"not found: {msg}"
                else:
                    detail = (f"failed to fetch anonymous token: unexpected status from GET request to https://{host}"
                              f"/artifactory/api/docker/{repo}/v2/token?scope=repository%3A"
                              f"{path.split(':')[0].replace('/', '%2F')}%3Apull&service={host} : 401")
                ev.append(f'Warning  Failed  kubelet  Failed to pull image "{image}": {detail}')
                ev.append("Warning  Failed  kubelet  Error: ErrImagePull")
                ev.append(f'Normal  BackOff  kubelet  Back-off pulling image "{image}"')
                ev.append("Warning  Failed  kubelet  Error: ImagePullBackOff")
        if ok:
            ev += ["Normal  Created  kubelet  Created container: test", "Normal  Started  kubelet  Started container test",
                   "Pod phase: Running"]
        self.ev("pull", "attempt", f"{name} ({sa}) on {n.name}: {'Running' if ok else 'ImagePullBackOff'}", node=n.name, ok=ok)
        pod = {"name": name, "image": image, "sa": sa, "node": n.name, "kind": kind, "ok": ok, "time": iso(self.t),
               "events": "Events:\n" + "\n".join("  " + e for e in ev)}
        self.pods[name] = pod
        self.evidence.setdefault("pods", []).append(pod)
        return pod

    def _credential(self, n, image, sa, pod_name):
        """kubelet → plugin → Artifactory. The JFrog username, or None (no credential)."""
        host = logic.image_host(image)
        if logic.JFROG_PROVIDER not in logic.providers_for_image(n.loaded, image):
            loaded = next((p for p in n.loaded if p["name"] == logic.JFROG_PROVIDER), None)
            self.because(f"{pod_name}: kubelet on {n.name} did not invoke the plugin: {host} is not in the loaded "
                         f"matchImages ({', '.join(loaded['matchImages']) if loaded else 'no JFrog entry loaded'})")
            return None
        entry = next(p for p in n.loaded if p["name"] == logic.JFROG_PROVIDER)
        cached = n.token_cache.get((sa, host))
        if cached and cached[0] > self.t:
            self.ev("token-cache", "hit", f"kubelet reused the cached credential for {sa}@{host}", node=n.name, ok=True)
            self.because(f"{pod_name}: kubelet reused a cached credential; the plugin was not called")
            return cached[1]
        env = logic.entry_env(entry)
        self.ev("plugin", "invoke", f"plugin invoked for {host}", node=n.name)
        if n.binary_bytes is not None and n.binary_bytes < logic.MIN_BINARY_BYTES:
            n.journal.append(self._journal_exec_failed(n, image))
            self.because(f"{pod_name}: the plugin binary on {n.name} is {n.binary_bytes} bytes; kubelet could not exec it")
            self.ev("plugin", "exec-error", "exec failed (empty binary)", node=n.name, ok=False)
            return None
        self._plog(n, "Auto-update functionality is disabled")
        self._plog(n, f"getting envs - azureAppAudience: {env.get('azure_app_audience')}, "
                      f"jfrogOidcProviderName: {env.get('jfrog_oidc_provider_name')}")
        if not self.sa_annotated.get(sa):
            self._plog(n, f"ERROR service account {sa} lacks required annotation JFrogExchange; no projected token", "error")
            n.journal.append(self._journal_exec_failed(n, image))
            self.because(f"{pod_name}: service account {sa} has no JFrogExchange annotation, so kubelet passed no "
                         f"token and the plugin failed")
            self.ev("plugin", "refused", f"{sa} not annotated", node=n.name, ok=False)
            return None
        self._plog(n, "Service Account Token obtained using Pod Identity (Kubernetes Workload Identity)")
        url = logic.token_url(env.get("artifactory_url"))
        self._plog(n, f"RT oidc token url :{url}")
        if logic.scheme_count(url) > 1:
            bad = url.replace("https://https://", "https://https//")
            self._plog(n, f'ERROR createArtifactoryToken failed Cause Post "{bad}": dial tcp: lookup https on '
                          f'168.63.129.16:53: no such host', "error")
            n.journal.append(self._journal_exec_failed(n, image))
            self.ev("plugin", "dns-error", "doubled scheme: DNS lookup of 'https' failed", node=n.name, ok=False)
            self.because(f"{pod_name}: the plugin on {n.name} built {url} from artifactory_url="
                         f"{env.get('artifactory_url')}; DNS failed before any exchange")
            return None
        status, who = self.art.exchange(self.t, env.get("jfrog_oidc_provider_name"),
                                        {"iss": self.issuer, "sub": f"system:serviceaccount:{self.cfg['k8s']['namespace']}:{sa}"})
        self.ev("artifactory", "exchange", f"POST /access/api/v1/oidc/token → {status}", ok=status == 200)
        if status != 200:
            self._plog(n, f"ERROR createArtifactoryToken failed: oidc token request returned status {status}: {who}", "error")
            n.journal.append(self._journal_exec_failed(n, image))
            self.because(f"{pod_name}: Artifactory refused the exchange ({status}: {who})")
            return None
        cache = logic.duration_seconds(entry.get("defaultCacheDuration")) or 0
        n.token_cache[(sa, host)] = (self.t + cache, who)
        self.ev("token-cache", "window", f"credential cached for {logic.human_duration(cache)}", node=n.name,
                until=round(self.t + cache, 3))
        self.because(f"{pod_name}: exchange succeeded as {who}")
        return who

    def _plog(self, n, msg, level="info"):
        n.plugin_log.append(json.dumps({"level": level, "time": iso(self.t), "msg": msg}))

    def _journal_exec_failed(self, n, image):
        return (f"{iso(self.t)} {n.name} kubelet: Failed to provide credentials for image \"{image}\": error execing "
                f"credential provider plugin jfrog-credentials-provider for image \"{image}\": exit status 1:")

    def remove_image(self, node=None):
        for n in self.nodes.values():
            if node is None or n.name == node:
                n.images.clear()

    def restart_kubelet(self, node=None):
        for n in list(self.nodes.values()):
            if node is None or n.name == node:
                self._kubelet_restart(n)

    def annotate_sa(self, sa, on=True):
        self.sa_annotated[sa] = on

    # ------------------------------------------------------------ diagnostics output

    def check_kubelet_provider(self, select="pod"):
        """CHECK_KUBELET_PROVIDER: inspect one node and print the QUICK SUMMARY footer.
        select="pod": the node of the test pod or a provider pod (the fixed scenario);
        select="node0": the first node (the first version's behaviour)."""
        first = sorted(self.nodes.values(), key=lambda x: x.name)[0]
        pod = self.pods.get(self.cfg["k8s"]["test_pod"])
        if select == "node0":
            n, reason = first, "node 0 (first node in the list)"
        elif pod and pod["node"] in self.nodes:
            n, reason = self.nodes[pod["node"]], f"node of pod {self.cfg['k8s']['namespace']}/{pod['name']}"
        elif self.ds_ready and self.labelled_nodes():
            n, reason = self.labelled_nodes()[0], "node of a JFrog provider pod"
        else:
            n, reason = first, "first node: no JFrog provider pod or test pod found"
        self.t += 3
        text = self.render_kubelet_check(n, reason)
        self.kubelet_checks.append(text)
        self.ev("pipeline", "check", f"CHECK_KUBELET_PROVIDER inspected {n.name} ({reason})", node=n.name)
        return text

    def render_kubelet_check(self, n, reason):
        jfrog = next((p for p in n.config if p["name"] == logic.JFROG_PROVIDER), None)
        entry_text = kubelet_config.render([jfrog]).split("providers:\n", 1)[1].rstrip("\n") if jfrog else ""
        urls = [l for l in n.plugin_log if "RT oidc token url" in l][-10:]
        bins = "acr-credential-provider" + (f", jfrog-credentials-provider ({n.binary_bytes} bytes)"
                                            if n.binary_bytes is not None and jfrog else "")
        return "\n".join([
            "===== kubelet flags =====",
            "/usr/local/bin/kubelet --image-credential-provider-config=/var/lib/kubelet/credential-provider-config.yaml "
            "--image-credential-provider-bin-dir=/var/lib/kubelet/credential-provider",
            "===== node config =====",
            kubelet_config.render(n.config),
            "===== plugin log (last 10) =====",
            *n.plugin_log[-10:],
            "===== kubelet journal (windowed) =====",
            *n.journal[-5:],
            "================ QUICK SUMMARY ================",
            f"Node inspected: {n.name}",
            f"Node chosen because: {reason}",
            "Kubelet flags: --image-credential-provider-config=/var/lib/kubelet/credential-provider-config.yaml "
            "--image-credential-provider-bin-dir=/var/lib/kubelet/credential-provider",
            f"Kubelet started at: {iso(n.kubelet_started)}",
            f"Config file mtime: {iso(n.mtime)}",
            f"Providers in config: {', '.join(p['name'] for p in n.config)}",
            f"Bin dir: {bins}",
            "-- jfrog provider entry --",
            entry_text,
            "-- latest OIDC token URLs --",
            *urls,
            "===============================================",
        ])

    def check_pod_config(self):
        """CHECK_JFROG_POD_CONFIG. Restarts the DaemonSet as a side effect."""
        v = self.release["values"] if self.release else self.values()
        sa = self.cfg["k8s"]["service_account"]
        text = "\n".join([
            "===== helm get values -a =====", model.render_values({**self.cfg, "values": v}),
            "===== rendered artifactory_url =====", "        - name: artifactory_url", f'          value: "{v["artifactory_url"]}"',
            "===== chart =====", "NAME                 NAMESPACE  REVISION  STATUS    CHART                            APP VERSION",
            f"{self.cfg['k8s']['release']}  {self.cfg['k8s']['namespace']}  {(self.release or {}).get('revision', 0)}  deployed  "
            f"jfrog-credential-provider-{v['chart_version']}  {v['chart_version']}",
            "===== service account =====", "apiVersion: v1", "kind: ServiceAccount", "metadata:", f"  name: {sa}",
            f"  namespace: {self.cfg['k8s']['namespace']}", "  annotations:",
            f'    JFrogExchange: "{"true" if self.sa_annotated.get(sa) else "false"}"',
            "===== restart =====", f"daemonset.apps/{self.cfg['k8s']['daemonset']} restarted"])
        self.evidence["pod_config"] = text
        self.restart_daemonset("CHECK_JFROG_POD_CONFIG side effect")
        return text

    def render_manifest(self, v):
        mi = "\n".join(f'        - "{m}"' for m in v["match_images"])
        return (f"---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {self.cfg['k8s']['release']}-config\n  labels:\n"
                f"    helm.sh/chart: jfrog-credential-provider-{v['chart_version']}\ndata:\n  provider-config.yaml: |\n"
                f"    - name: jfrog-credentials-provider\n      matchImages:\n{mi}\n      defaultCacheDuration: "
                f"{v['default_cache_duration']}\n      env:\n        - name: artifactory_url\n"
                f"          value: \"{v['artifactory_url']}\"\n        - name: jfrog_oidc_provider_name\n"
                f"          value: \"{v['provider_name']}\"\n")

    # ------------------------------------------------------------ Phase 3b tools

    def probe_realm(self, host):
        repo = self.art.repo_from_host(host)
        hdr = f'Bearer realm="https://{host}/artifactory/api/docker/{repo or "null"}/v2/token",service="{host}"'
        self.ev("artifactory", "probe", f"HEAD https://{host}/v2/ → 401", ok=repo is not None)
        if repo is None:
            self.because(f"realm probe of {host}: the realm does not name the repo key (bare domain)")
        res = {"host": host, "status": 401, "www_authenticate": hdr}
        self.evidence.setdefault("probes", []).append(res)
        return res

    def promote(self, source, target, docker_repository, tag):
        src = self.art.repos.get(source)
        path = f"{docker_repository}:{tag}"
        if not src or src["type"] != "local":
            res = {"status": 400, "body": f"Unsupported V2 repository request for '{source}'"}
            self.because(f"Promote from {source} failed: Promote needs a local source repository")
        elif path not in src["images"]:
            res = {"status": 404, "body": "tag not found"}
        else:
            self.art.repos[target]["images"].add(path)          # Promote keeps the source path
            res = {"status": 200, "body": "Promotion ended successfully", "landed": f"{target}/{docker_repository}/{tag}"}
        self.ev("artifactory", "promote", f"promote {source} → {target}: {res['status']}", ok=res["status"] == 200)
        self.evidence.setdefault("promotes", []).append({"source": source, "target": target, **res})
        return res

    def aql(self, prefix="mcp/sonarqube"):
        hits = [{"repo": k, "path": p.split(":")[0], "name": p.split(":")[1]}
                for k, r in self.art.repos.items() if r["type"] == "local" for p in sorted(r["images"]) if p.startswith(prefix)]
        self.evidence["aql"] = hits
        return hits

    def pull_remote(self, repo="example-docker-remote"):
        r = self.art.repos[repo]
        res = ({"status": 302, "location": f"https://landing.jfrog.com/reactivate-server/{repo}"} if r.get("dormant")
               else {"status": 200, "location": ""})
        if r.get("dormant"):
            self.because(f"{repo} is dormant: JFrog Cloud paused the idle repo (302 to reactivate-server)")
        self.ev("artifactory", "remote", f"GET via {repo} → {res['status']}", ok=res["status"] == 200)
        self.evidence["remote"] = res
        return res

    # ------------------------------------------------------------ evidence bundle

    def bundle(self) -> dict:
        """Everything the checks and the rule engine read, in pipeline-output form."""
        v = self.release["values"] if self.release else self.values()
        return {
            "config": self.cfg,
            "values_file": model.render_values(self.cfg),
            "helm_values": model.render_values({**self.cfg, "values": v}) if self.release else "",
            "manifest": self.render_manifest(v) if self.release else "",
            "kubelet_checks": list(self.kubelet_checks),
            "pods": self.evidence.get("pods", []),
            "pod_config": self.evidence.get("pod_config", ""),
            "install_logs": self.install_logs,
            "reset_logs": self.reset_logs,
            "mirror": self.evidence.get("mirror"),
            "probes": self.evidence.get("probes", []),
            "promotes": self.evidence.get("promotes", []),
            "remote": self.evidence.get("remote"),
            "audit": list(self.art.audit),
            "providers": list(self.art.providers.values()),
            "mappings": [dict(m, provider=p) for p, ms in self.art.mappings.items() for m in ms],
            "daemonsets": [{"namespace": o["namespace"], "name": o["name"], "age_days": o["age_days"],
                            "mounts": [logic.NODE_CONFIG], "deleted": o.get("deleted", False),
                            "match_images": o["entry"]["matchImages"],
                            "provider_name": logic.entry_env(o["entry"]).get("jfrog_oidc_provider_name")}
                           for o in self.orphans],
            "node_created": {n.name: iso(n.created) for n in self.nodes.values()},
            "install_time": iso(self.t) if self.release else "",
        }
