"""Knative services on OpenShift Serverless or any Kubernetes cluster with Knative Serving.

Identity is the Knative service's Kubernetes service account. The workload reads
a projected service-account token with the JFrog audience from a file
(JFROG_OIDC_SOURCE=file); the kubelet refreshes it. JFrog trusts the cluster's
service-account issuer, so the issuer's discovery document and JWKS must be
reachable from JFrog: Preflight and Discover fail clearly when they are not.

Every call is plain `kubectl ... --context <ctx>` (oc accepts the same), argv
only, through the console's Shell. Status: simulation and fixtures only; no live
cluster has been used (CLAUDE.md, Known unverified items).
"""

from __future__ import annotations

import ipaddress
import json
import urllib.parse

from .. import model

TOKEN_DIR = "/var/run/secrets/jfrog"
TOKEN_FILE = f"{TOKEN_DIR}/token"
VOLUME = "jfrog-oidc-token"
TOKEN_SECONDS = 3600
RESOLVE = "@resolve:"
PRIVATE_ISSUERS = ("kubernetes.default.svc", "kubernetes.default", "localhost")


def ctx_args(cfg) -> list:
    return ["--context", str(model.get_path(cfg, "target.openshift.context") or "")]


def issuer_problem(issuer: str):
    """Why JFrog cannot use this issuer, or None."""
    parts = urllib.parse.urlsplit(issuer or "")
    host = parts.hostname or ""
    if parts.scheme != "https":
        return "is not an https URL"
    if host in PRIVATE_ISSUERS or host.endswith((".svc", ".svc.cluster.local", ".local", ".internal")):
        return "is the cluster-internal default, which JFrog cannot reach"
    try:
        if ipaddress.ip_address(host).is_private:
            return "is a private IP address"
    except ValueError:
        pass
    return None


ISSUER_FIX = ("Give the cluster a public service-account issuer and publish its JWKS (OpenShift: set "
              "spec.serviceAccountIssuer on authentication.config.openshift.io/cluster and serve "
              "/.well-known/openid-configuration and the keys from a public URL, e.g. ROSA/STS's OIDC bucket; "
              "other clusters: --service-account-issuer and --service-account-jwks-uri on the API server).")


def _issuer(step, cfg) -> dict:
    """The cluster's discovery document via the API server (read-only)."""
    res = step.sh.run(["kubectl", "get", "--raw", "/.well-known/openid-configuration", *ctx_args(cfg)], timeout=60)
    return (res.json() or {}) if res.ok else {}


# ------------------------------------------------------------------- Preflight


class OpenShiftPreflight:
    def openshift(self):
        o = model.get_path(self.cfg, "target.openshift") or {}
        res = self.sh.run(["kubectl", "version", "--client", "-o", "json"], timeout=60)
        if not res.ok:
            return self.add("tool_kubectl", "kubectl installed", "fail",
                            "kubectl not found on PATH" if res.rc == 127 else res.error_text(),
                            "Install kubectl (or use the OpenShift CLI's bundled kubectl).")
        self.add("tool_kubectl", "kubectl installed", "ok",
                 ((res.json() or {}).get("clientVersion") or {}).get("gitVersion", "?"))
        res = self.sh.run(["kubectl", "config", "get-contexts", "-o", "name"], timeout=30)
        if o.get("context") not in res.stdout.split():
            return self.add("k8s_context", f"kube context {o.get('context')}", "fail", "not in the kubeconfig",
                            "Log in to the cluster (oc login / your provider's get-credentials) and pick its context.")
        res = self.sh.run(["kubectl", "get", "crd", "services.serving.knative.dev", *ctx_args(self.cfg), "-o", "name"],
                          timeout=60)
        if not res.ok:
            return self.add("knative", "Knative Serving installed", "fail", res.error_text(200),
                            "Install OpenShift Serverless (Knative Serving), or Knative Serving upstream.")
        self.add("knative", "Knative Serving installed", "ok", "services.serving.knative.dev")
        self._k8s_issuer()
        if not self.delivery:
            self._k8s_rights(o)

    def _k8s_issuer(self):
        title = "Service-account issuer reachable by JFrog"
        doc = _issuer(self, self.cfg)
        iss = str(doc.get("issuer") or "").rstrip("/")
        if not iss:
            return self.add("k8s_issuer", title, "fail", "the API server has no /.well-known/openid-configuration",
                            ISSUER_FIX)
        problem = issuer_problem(iss)
        if problem:
            return self.add("k8s_issuer", title, "fail", f"issuer {iss} {problem}", ISSUER_FIX)
        self.facts["k8s_issuer"] = iss
        pub = self.sh.http("GET", f"{iss}/.well-known/openid-configuration", timeout=15)
        jwks = (pub.json() or {}).get("jwks_uri") if pub.ok else None
        keys = self.sh.http("GET", jwks, timeout=15) if jwks and jwks.startswith("https://") else None
        if not (keys and keys.ok and (keys.json() or {}).get("keys")):
            return self.add("k8s_issuer", title, "fail",
                            f"{iss}/.well-known/openid-configuration or its jwks_uri is not publicly reachable "
                            f"({pub.status or pub.error})", ISSUER_FIX)
        self.add("k8s_issuer", title, "ok", f"{iss} (JWKS {len((keys.json() or {})['keys'])} key(s))")

    def _k8s_rights(self, o):
        ns = o.get("namespace")
        missing = []
        for verb, res in (("patch", "services.serving.knative.dev"), ("create", "serviceaccounts")):
            r = self.sh.run(["kubectl", "auth", "can-i", verb, res, "-n", ns, *ctx_args(self.cfg)], timeout=30)
            if r.stdout.strip() != "yes":
                missing.append(f"{verb} {res}")
        if missing:
            return self.add("k8s_permissions", f"Rights in namespace {ns}", "warn", "cannot " + ", ".join(missing),
                            "Ask for edit rights on the namespace; Plan and Wire fail without them.")
        self.add("k8s_permissions", f"Rights in namespace {ns}", "ok", "patch ksvc, create serviceaccounts")


# -------------------------------------------------------------------- Discover


class OpenShiftDiscover:
    def openshift(self):
        o = model.get_path(self.cfg, "target.openshift") or {}
        if not all(o.get(k) for k in ("context", "cluster", "namespace", "name")):
            return self.error("missing_fields", "context, cluster, namespace and name are required")
        self.facts.update(context=o["context"], cluster=o["cluster"], namespace=o["namespace"], name=o["name"])
        doc = _issuer(self, self.cfg)
        iss = str(doc.get("issuer") or "").rstrip("/")
        problem = issuer_problem(iss) if iss else "is not published by the API server"
        if problem:
            return self.error("issuer_unreachable", f"the cluster's service-account issuer {iss or ''} {problem}".replace("  ", " "),
                              ISSUER_FIX)
        self.facts["issuer"] = iss
        res = self.sh.run(["kubectl", "get", "ksvc", o["name"], "-n", o["namespace"], "-o", "json", *ctx_args(self.cfg)],
                          timeout=60, sensitive=True)     # env values may be secrets
        if not res.ok:
            text = res.error_text(300)
            code = "not_found" if "NotFound" in text or "not found" in text else "lookup_failed"
            return self.error(code, f"Knative service {o['namespace']}/{o['name']}: {text}",
                              "Check the name, namespace and context.")
        ksvc = res.json() or {}
        spec = ((ksvc.get("spec") or {}).get("template") or {}).get("spec") or {}
        labels = (ksvc.get("metadata") or {}).get("labels") or {}
        self.facts.update(image=((spec.get("containers") or [{}])[0]).get("image"),
                          url=(ksvc.get("status") or {}).get("url"),
                          function=any(k.startswith("function.knative.dev") for k in labels))
        sa = spec.get("serviceAccountName") or "default"
        self.facts["service_account"] = sa
        dedicated = model.dedicated_identity_name(self.cfg)
        if sa == dedicated:
            self.facts.update(dedicated_identity=dedicated, mapping_subject=None)
        elif sa == "default" or self._sa_users(o, sa) > 1:
            self.facts.update(dedicated_identity=dedicated, mapping_subject=None)
            self.warn("dedicated_identity",
                      f"{o['name']} runs as {'the namespace default' if sa == 'default' else 'the shared'} service account "
                      f"{sa}; Wire switches it to a new {dedicated}",
                      "The new account has no RBAC roles: grant it what the app needs before you approve Wire.")
        else:
            self.facts["mapping_subject"] = f"system:serviceaccount:{o['namespace']}:{sa}"
        if model.get_path(self.cfg, "jfrog.integration_names.openshift") == model.DEFAULT_INTEGRATIONS["openshift"]:
            self.patch["jfrog.integration_names.openshift"] = f"openshift-{o['cluster']}"

    def _sa_users(self, o, sa) -> int:
        res = self.sh.run(["kubectl", "get", "ksvc", "-n", o["namespace"], "-o",
                           "jsonpath={.items[*].spec.template.spec.serviceAccountName}", *ctx_args(self.cfg)], timeout=60)
        return res.stdout.split().count(sa) if res.ok else 1


# ------------------------------------------------------------------------ Wire


class OpenShiftWire:
    def _k8s_env(self) -> dict:
        provider, audience = self._need("jfrog_provider_name", "jfrog_audience")
        return {"JFROG_OIDC_AUDIENCE": audience, "JFROG_OIDC_PROVIDER": provider, "JFROG_OIDC_SOURCE": "file",
                "JFROG_OIDC_TOKEN_FILE": TOKEN_FILE, "JFROG_URL": self.jfrog_url}

    def _k8s_volume(self):
        return {"name": VOLUME, "projected": {"sources": [{"serviceAccountToken": {
            "audience": self.out["jfrog_audience"], "expirationSeconds": TOKEN_SECONDS, "path": "token"}}]}}

    def _k8s_get(self):
        o = model.get_path(self.cfg, "target.openshift")
        res = self.sh.run(["kubectl", "get", "ksvc", o["name"], "-n", o["namespace"], "-o", "json", *ctx_args(self.cfg)],
                          timeout=60, sensitive=True)
        if not res.ok:
            from .wire import WireError
            raise WireError(f"could not read the target: {res.error_text(300)}")
        return res.json() or {}

    def _k8s_target_id(self):
        o = model.get_path(self.cfg, "target.openshift")
        return f"k8s:{o['cluster']}:{o['namespace']}/ksvc/{o['name']}"

    def _k8s_template(self, ksvc, env):
        """The ksvc's spec.template with the managed settings applied (whole lists: a merge patch replaces them)."""
        tpl = json.loads(json.dumps(((ksvc.get("spec") or {}).get("template")) or {}))
        spec = tpl.setdefault("spec", {})
        c = (spec.setdefault("containers", [{}]) or [{}])[0]
        c["env"] = [e for e in c.get("env") or [] if e.get("name") not in env] + \
                   [{"name": k, "value": v} for k, v in sorted(env.items())]
        c["volumeMounts"] = [m for m in c.get("volumeMounts") or [] if m.get("name") != VOLUME] + \
                            [{"name": VOLUME, "mountPath": TOKEN_DIR, "readOnly": True}]
        spec["volumes"] = [v for v in spec.get("volumes") or [] if v.get("name") != VOLUME] + [self._k8s_volume()]
        if self.dedicated:
            spec["serviceAccountName"] = self.dedicated["id"]
        # A new revision name would collide with the current one: let Knative name it.
        (tpl.get("metadata") or {}).pop("name", None)
        return tpl

    def _openshift(self, env):
        ksvc = self._k8s_get()
        spec = ((ksvc.get("spec") or {}).get("template") or {}).get("spec") or {}
        c = (spec.get("containers") or [{}])[0]
        cur_env = {e.get("name"): e.get("value", "[valueFrom]") for e in c.get("env") or []}
        diff = [{"scope": "env", "setting": k, "current": cur_env.get(k), "desired": v,
                 "action": "same" if cur_env.get(k) == v else "add" if k not in cur_env else "change"} for k, v in env.items()]
        def obj(scope, setting, cur, want):
            show = lambda v: None if v is None else json.dumps(v, sort_keys=True)  # noqa: E731
            diff.append({"scope": scope, "setting": setting, "current": show(cur), "desired": show(want),
                         "action": "same" if cur == want else "add" if cur is None else "change"})
        obj("volume", VOLUME, next((v for v in spec.get("volumes") or [] if v.get("name") == VOLUME), None),
            self._k8s_volume())
        obj("volumeMount", TOKEN_DIR, next((m for m in c.get("volumeMounts") or [] if m.get("name") == VOLUME), None),
            {"name": VOLUME, "mountPath": TOKEN_DIR, "readOnly": True})
        if self.dedicated:
            sa = spec.get("serviceAccountName") or "default"
            diff.insert(0, {"scope": "identity", "setting": "serviceAccountName", "current": sa,
                            "desired": self.dedicated["id"], "action": "same" if sa == self.dedicated["id"] else "change"})
        o = model.get_path(self.cfg, "target.openshift")
        commands = []
        if any(d["action"] != "same" for d in diff):
            commands = [["kubectl", "patch", "ksvc", o["name"], "-n", o["namespace"], *ctx_args(self.cfg),
                         "--type", "merge", "-p", RESOLVE + "ksvc-template"]]
        others = len([k for k in cur_env if k not in env])
        notes = [f"A merge patch on spec.template creates a new Knative revision; traffic moves when it is ready."]
        if others:
            notes.append(f"{others} other environment variable(s) are kept as they are (not shown).")
        if self.dedicated:
            notes.append(f"The service moves to the service account {self.dedicated['id']}, which has no RBAC roles. "
                         "Grant it what the app needs.")
        notes.append("Add the volume, mount and variables to the service's own manifests so the next deploy keeps them.")
        return diff, commands, notes

    def _k8s_resolve(self, argv):
        if RESOLVE + "ksvc-template" not in argv:
            return argv
        patch = json.dumps({"spec": {"template": self._k8s_template(self._k8s_get(), self.desired_env())}},
                           sort_keys=True, separators=(",", ":"))
        return [patch if a == RESOLVE + "ksvc-template" else a for a in argv]
