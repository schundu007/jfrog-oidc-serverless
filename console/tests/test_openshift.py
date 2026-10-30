"""Knative services (OpenShift Serverless or any Knative cluster): Preflight, Discover, Wire.

All fixtures are doc-derived (Knative Serving v1 Service, Kubernetes service-account
issuer discovery); no cluster was used. OpenShift is simulation-only for now.
"""

import json
import unittest

from console import model
from console.shell import Shell
from console.steps import discover, openshift, preflight, wire

CTX = ["--context", "admin@lab"]
ISS = "https://oidc.lab.example.com"
SECRET = "not-for-the-console"
OUT = {"jfrog_provider_name": "openshift-lab", "jfrog_audience": "jfrog-k8s"}


def cfg(name="orders"):
    c = model.default_config("openshift_knative_service", name=name)
    c["target"]["openshift"].update(context="admin@lab", cluster="lab", namespace="shop", name=name)
    c["jfrog"]["url"] = "https://acme.jfrog.io"
    return c


def ksvc(sa=None, env=(), volumes=None, mounts=None):
    spec = {"containers": [{"image": "quay.io/acme/orders:1", "env": list(env), **({"volumeMounts": mounts} if mounts else {})}]}
    if sa:
        spec["serviceAccountName"] = sa
    if volumes:
        spec["volumes"] = volumes
    return {"apiVersion": "serving.knative.dev/v1", "kind": "Service",
            "metadata": {"name": "orders", "namespace": "shop", "labels": {}},
            "spec": {"template": {"metadata": {"name": "orders-00003"}, "spec": spec}},
            "status": {"url": "https://orders-shop.apps.lab.example.com"}}


def fx_get(obj, consume=False):
    return {"argv": ["kubectl", "get", "ksvc", "orders", "-n", "shop", "-o", "json", *CTX], "stdout": json.dumps(obj),
            "consume": consume}


ISSUER_FX = {"argv": ["kubectl", "get", "--raw", "/.well-known/openid-configuration", *CTX],
             "stdout": json.dumps({"issuer": ISS, "jwks_uri": f"{ISS}/openid/v1/jwks"})}


class IssuerTests(unittest.TestCase):
    def test_problems(self):
        self.assertIsNone(openshift.issuer_problem(ISS))
        self.assertIn("cluster-internal", openshift.issuer_problem("https://kubernetes.default.svc"))
        self.assertIn("https", openshift.issuer_problem("http://oidc.example.com"))
        self.assertIn("private", openshift.issuer_problem("https://10.0.0.5"))


class DiscoverTests(unittest.TestCase):
    def run_d(self, obj, extra=()):
        sh = Shell(mode="replay", fixtures=[ISSUER_FX, fx_get(obj), *extra])
        return discover.run(cfg(), sh), sh

    def test_own_service_account(self):
        users = {"argv": ["kubectl", "get", "ksvc", "-n", "shop", "-o",
                          "jsonpath={.items[*].spec.template.spec.serviceAccountName}", *CTX], "stdout": "orders-sa other"}
        res, sh = self.run_d(ksvc(sa="orders-sa", env=[{"name": "DB_PASSWORD", "value": SECRET}]), [users])
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["facts"]["mapping_subject"], "system:serviceaccount:shop:orders-sa")
        self.assertEqual(res["facts"]["issuer"], ISS)
        self.assertEqual(res["config_patch"]["jfrog.integration_names.openshift"], "openshift-lab")
        self.assertNotIn(SECRET, json.dumps(res))

    def test_default_sa_gets_dedicated(self):
        res, _ = self.run_d(ksvc())
        self.assertEqual(res["facts"]["dedicated_identity"], "jfrog-orders")
        self.assertIsNone(res["facts"]["mapping_subject"])

    def test_internal_issuer_fails_with_fix(self):
        sh = Shell(mode="replay", fixtures=[{**ISSUER_FX, "stdout": json.dumps({"issuer": "https://kubernetes.default.svc"})}])
        res = discover.run(cfg(), sh)
        self.assertEqual(res["errors"][0]["code"], "issuer_unreachable")
        self.assertIn("serviceAccountIssuer", res["errors"][0]["fix"])


class PreflightTests(unittest.TestCase):
    def fixtures(self, jwks_status=200):
        return [
            {"argv": ["terraform", "version", "-json"], "stdout": json.dumps({"terraform_version": "1.9.8"})},
            {"argv": ["kubectl", "version", "--client", "-o", "json"],
             "stdout": json.dumps({"clientVersion": {"gitVersion": "v1.34.1"}})},
            {"argv": ["kubectl", "config", "get-contexts", "-o", "name"], "stdout": "admin@lab\nkind-dev\n"},
            {"argv": ["kubectl", "get", "crd", "services.serving.knative.dev", *CTX, "-o", "name"],
             "stdout": "customresourcedefinition.apiextensions.k8s.io/services.serving.knative.dev\n"},
            ISSUER_FX,
            {"http": f"GET {ISS}/.well-known/openid-configuration", "status": 200,
             "body": json.dumps({"issuer": ISS, "jwks_uri": f"{ISS}/openid/v1/jwks"})},
            {"http": f"GET {ISS}/openid/v1/jwks", "status": jwks_status, "body": json.dumps({"keys": [{"kid": "a"}]})},
            {"argv": ["kubectl", "auth", "can-i", "patch", "services.serving.knative.dev", "-n", "shop", *CTX], "stdout": "yes\n"},
            {"argv": ["kubectl", "auth", "can-i", "create", "serviceaccounts", "-n", "shop", *CTX], "stdout": "yes\n"},
            {"http": "GET https://acme.jfrog.io/access/api/v1/system/ping", "status": 200, "body": "OK"},
            {"http": "GET https://acme.jfrog.io/access/api/v1/oidc", "status": 200, "body": "[]"},
            {"http": "GET https://acme.jfrog.io/access/api/v2/groups/serverless-readers", "status": 200, "body": "{}"},
        ]

    def test_all_green(self):
        sh = Shell(mode="replay", fixtures=self.fixtures())
        res = preflight.run(cfg(), sh, env={"JFROG_ACCESS_TOKEN": "t" * 20})
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["status"], "ok", [c for c in res["checks"] if c["status"] != "ok"])
        self.assertEqual(res["facts"]["k8s_issuer"], ISS)

    def test_jwks_not_public_fails(self):
        res = preflight.run(cfg(), Shell(mode="replay", fixtures=self.fixtures(jwks_status=403)),
                            env={"JFROG_ACCESS_TOKEN": "t" * 20})
        c = {c["id"]: c for c in res["checks"]}["k8s_issuer"]
        self.assertEqual(c["status"], "fail")
        self.assertIn("publicly reachable", c["detail"])


class WireTests(unittest.TestCase):
    def wired(self, **facts):
        c = cfg()
        c["target"]["discovered"] = {"kind": "openshift_knative_service",
                                     "mapping_subject": "system:serviceaccount:shop:orders-sa", **facts}
        return c

    def test_plan_patch_and_no_secret(self):
        sh = Shell(mode="replay", fixtures=[fx_get(ksvc(sa="orders-sa", env=[{"name": "DB_PASSWORD", "value": SECRET}]))])
        w = wire.Wire(self.wired(), sh, OUT)
        plan = w.plan()
        self.assertNotIn(SECRET, json.dumps(plan))
        self.assertEqual(plan["commands"][0][:3], ["kubectl", "patch", "ksvc"])
        self.assertIn("merge", plan["commands"][0])
        patch = json.loads(w._resolve(plan["commands"][0])[-1])
        tpl = patch["spec"]["template"]
        self.assertNotIn("name", tpl.get("metadata", {}))
        c = tpl["spec"]["containers"][0]
        env = {e["name"]: e["value"] for e in c["env"]}
        self.assertEqual(env["DB_PASSWORD"], SECRET)          # kept, sent to the cluster only
        self.assertEqual(env["JFROG_OIDC_SOURCE"], "file")
        self.assertEqual(env["JFROG_OIDC_TOKEN_FILE"], "/var/run/secrets/jfrog/token")
        self.assertEqual(c["image"], "quay.io/acme/orders:1")
        vol = tpl["spec"]["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"]
        self.assertEqual((vol["audience"], vol["expirationSeconds"]), ("jfrog-k8s", 3600))

    def test_dedicated_sa_switch(self):
        c = self.wired(dedicated_identity="jfrog-orders", mapping_subject=None)
        sh = Shell(mode="replay", fixtures=[fx_get(ksvc())])
        w = wire.Wire(c, sh, {**OUT, "dedicated_service_accounts": {"orders": "jfrog-orders"}})
        plan = w.plan()
        self.assertEqual(plan["diff"][0]["setting"], "serviceAccountName")
        patch = json.loads(w._resolve(plan["commands"][0])[-1])
        self.assertEqual(patch["spec"]["template"]["spec"]["serviceAccountName"], "jfrog-orders")

    def test_apply_then_noop(self):
        c = self.wired()
        w0 = wire.Wire(c, Shell(mode="replay", fixtures=[fx_get(ksvc(sa="orders-sa"))]), OUT)
        after = ksvc(sa="orders-sa")
        after["spec"]["template"] = w0._k8s_template(ksvc(sa="orders-sa"), w0.desired_env())
        patch_argv = ["kubectl", "patch", "ksvc", "orders", "-n", "shop", *CTX, "--type", "merge", "-p",
                      json.dumps({"spec": {"template": after["spec"]["template"]}}, sort_keys=True, separators=(",", ":"))]
        sh = Shell(mode="replay", fixtures=[fx_get(ksvc(sa="orders-sa"), True), fx_get(ksvc(sa="orders-sa"), True),
                                            fx_get(ksvc(sa="orders-sa"), True), {"argv": patch_argv, "stdout": "patched"},
                                            fx_get(after)])
        w = wire.Wire(c, sh, OUT)
        plan = w.plan()
        res = w.apply(plan, {"diff_sha256": plan["sha256"]})
        self.assertEqual(sh.misses, [])
        self.assertTrue(res["verified"])


if __name__ == "__main__":
    unittest.main()
