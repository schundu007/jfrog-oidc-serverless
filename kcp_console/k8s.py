"""Read-only kubectl and helm (spec §1, execution model).

The console never runs a state-changing kubectl or helm as the operator: this
module refuses anything but kubectl get / auth can-i / version and helm get /
list / history / status, before a process starts. Personal accounts are
read-only and namespaced, so a Forbidden answer is normal: the result names the
read-only pipeline scenario that gets the same facts instead.
"""

from __future__ import annotations

import re

from .parse import events

KUBECTL_VERBS = {("get",), ("auth", "can-i"), ("version",)}
HELM_VERBS = {"get", "list", "history", "status"}
FORBIDDEN = re.compile(r"\bForbidden\b|forbidden:|does not have access to the resource in Azure")

# Which read-only pipeline scenario answers a read the operator's RBAC refuses.
FALLBACK = {"nodes": "CHECK_KUBELET_PROVIDER", "daemonsets": "FIND_CREDENTIAL_PROVIDER_WRITERS",
            "helm": "CHECK_JFROG_POD_CONFIG", "pods": "CHECK_KUBELET_PROVIDER", "serviceaccount": "CHECK_JFROG_POD_CONFIG"}


class ReadOnlyError(ValueError):
    pass


def check_kubectl(args: list) -> None:
    if not any(tuple(args[:len(v)]) == v for v in KUBECTL_VERBS):
        raise ReadOnlyError(f"kubectl {' '.join(args[:2])} is not a read: the console only runs get, auth can-i and version")


def check_helm(args: list) -> None:
    if not args or args[0] not in HELM_VERBS:
        raise ReadOnlyError(f"helm {args[0] if args else ''} is not a read: the console only runs get, list, history and status")


class Kube:
    def __init__(self, shell, context: str | None = None, kubeconfig=None):
        self.sh, self.context = shell, context
        self.env = {"KUBECONFIG": str(kubeconfig)} if kubeconfig else None

    def _read(self, what, argv, parse_json=True):
        res = self.sh.run(argv, timeout=60, extra_env=self.env)
        out = {"ok": res.ok, "argv": argv, "what": what}
        if res.ok:
            out["data"] = res.json() if parse_json else res.stdout
            return out
        text = res.error_text(1200)
        out["error"] = text
        if FORBIDDEN.search(text):
            fb = events.parse_pod_events(text)["forbidden"]
            out["forbidden"] = True
            out["user_principal"] = fb[0]["user_principal"] if fb else None
            out["fallback"] = FALLBACK.get(what)
        return out

    def kubectl(self, what, *args, parse_json=True):
        args = list(args)
        check_kubectl(args)
        ctx = ["--context", self.context] if self.context else []
        return self._read(what, ["kubectl", *args, *ctx], parse_json)

    def helm(self, what, *args, parse_json=True):
        args = list(args)
        check_helm(args)
        ctx = ["--kube-context", self.context] if self.context else []
        return self._read(what, ["helm", *args, *ctx], parse_json)

    # -- the reads Discover and Preflight use

    def daemonsets(self):
        return self.kubectl("daemonsets", "get", "daemonsets", "-A", "-o", "json")

    def service_account(self, ns, name):
        return self.kubectl("serviceaccount", "get", "serviceaccount", name, "-n", ns, "-o", "json")

    def nodes(self):
        return self.kubectl("nodes", "get", "nodes", "-o", "json")

    def pods(self, ns):
        return self.kubectl("pods", "get", "pods", "-n", ns, "-o", "json")

    def releases(self, ns):
        return self.helm("helm", "list", "-n", ns, "-o", "json")

    def values(self, release, ns):
        return self.helm("helm", "get", "values", release, "-n", ns, "-a", "-o", "yaml", parse_json=False)

    def manifest(self, release, ns):
        return self.helm("helm", "get", "manifest", release, "-n", ns, parse_json=False)
