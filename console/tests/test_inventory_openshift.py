"""Inventory for kube contexts (Knative services). The ksvc list is doc-derived."""

import json
import unittest

from console import inventory
from console.shell import Result, Shell

KSVC = {"apiVersion": "v1", "kind": "List", "items": [
    {"apiVersion": "serving.knative.dev/v1", "kind": "Service", "metadata": {"name": "orders", "namespace": "shop"}},
    {"apiVersion": "serving.knative.dev/v1", "kind": "Service", "metadata": {"name": "hello", "namespace": "demo"}}]}
CTX = "shop/api-lab-example-com:6443/kube:admin"


class Fake(Shell):
    def __init__(self, out, fail=()):
        super().__init__(mode="replay")
        self.out, self.fail, self.calls = out, fail, []

    def _replay_cmd(self, argv):
        self.calls.append(list(argv))
        for prefix, value in self.out.items():
            if tuple(argv[:len(prefix)]) == prefix:
                if prefix in self.fail:
                    return Result(argv, 254, "", self.fail[prefix], 0.0)
                return Result(argv, 0, value if isinstance(value, str) else json.dumps(value), "", 0.0)
        self.misses.append(" ".join(argv))
        return Result(argv, 127, "", "no fixture", 0.0)


class KnativeInventoryTests(unittest.TestCase):
    VIEW = {"clusters": [{"name": "lab", "cluster": {"server": "https://api.lab.example.com:6443"}},
                         {"name": "aks", "cluster": {"server": "https://x.hcp.eastus2.azmk8s.io:443"}},
                         {"name": "gke", "cluster": {"server": "https://35.0.0.1"}}],
            "contexts": [{"name": CTX, "context": {"cluster": "lab"}},
                         {"name": "my-ocp", "context": {"cluster": "lab"}},
                         {"name": "trk-aks", "context": {"cluster": "aks"}},
                         {"name": "gke_p_us-central1_c1", "context": {"cluster": "gke"}},
                         {"name": "kind-dev", "context": {"cluster": "gke"}}]}
    OUT = {("kubectl", "config", "view"): VIEW,
           ("kubectl", "config", "current-context"): CTX + "\n",
           ("kubectl", "get", "ksvc"): KSVC}

    def test_lists_ksvc_in_every_namespace(self):
        res = inventory.run("openshift", Fake(self.OUT), None)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["defaults"]["context"], CTX)
        self.assertEqual(res["defaults"]["cluster"], "lab-example-com")
        self.assertEqual({(w["namespace"], w["name"]) for w in res["workloads"]}, {("shop", "orders"), ("demo", "hello")})

    def test_no_knative(self):
        sh = Fake(self.OUT, fail={("kubectl", "get", "ksvc"):
                                  'error: the server doesn\'t have a resource type "ksvc"'})
        res = inventory.run("openshift", sh, None)
        self.assertIn("Knative Serving is not installed", res["errors"][0])

    def test_only_openshift_contexts(self):
        res = inventory.run("openshift", Fake(self.OUT), None)
        self.assertEqual([c["id"] for c in res["scopes"]], sorted([CTX, "my-ocp"]))

    def test_no_openshift_context(self):
        out = {**self.OUT, ("kubectl", "config", "view"): {**self.VIEW, "contexts": self.VIEW["contexts"][2:]},
               ("kubectl", "config", "current-context"): "trk-aks\n"}
        sh = Fake(out)
        res = inventory.run("openshift", sh, None)
        self.assertEqual((res["scopes"], res["workloads"], res["defaults"]["context"]), ([], [], ""))
        self.assertIn("oc login", res["errors"][0])
        self.assertFalse([c for c in sh.calls if c[:2] == ["kubectl", "get"]])

    def test_cluster_slug(self):
        self.assertEqual(inventory.cluster_slug("kind-dev"), "kind-dev")
        self.assertEqual(inventory.cluster_slug("default/api-ocp-acme-io:6443/admin"), "ocp-acme-io")
        self.assertTrue(inventory.cluster_slug("123")[0].isalpha())


if __name__ == "__main__":
    unittest.main()
