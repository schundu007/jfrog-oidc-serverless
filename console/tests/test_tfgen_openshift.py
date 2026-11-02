"""Workspaces for Knative clusters (JSON syntax, constants-only main.tf.json).

    CONSOLE_TF_VALIDATE=1 PYTHONPATH=client/src:. python -m unittest console.tests.test_tfgen_openshift
"""

import json
import os
import shutil
import unittest

from console import model
from console.steps import tfgen
from console.tests.test_tfgen import Base, TerraformValidateTests

K8S_ISS = "https://oidc.example-cluster.openshiftapps.com"


def openshift_cfg(kind="openshift_knative_service", name="orders", sa=None, dedicated=False):
    cfg = model.default_config(kind, name=name)
    cfg["target"]["openshift"].update(context="admin@lab", cluster="lab", namespace="shop", name=name)
    cfg["jfrog"]["integration_names"]["openshift"] = "openshift-lab"
    d = {"kind": kind, "issuer": K8S_ISS,
         "mapping_subject": None if dedicated else f"system:serviceaccount:shop:{sa or name}"}
    if dedicated:
        d["dedicated_identity"] = model.dedicated_identity_name(cfg)
    cfg["target"]["discovered"] = d
    return cfg


class OpenShiftWorkspaceTests(Base):
    def test_mapping_subject_and_issuer(self):
        r = self.render(openshift_cfg())
        self.assertEqual(r["key"], "openshift:lab")
        tv = self.files(r)["terraform.tfvars.json"]
        self.assertEqual(tv["workloads"]["orders-runtime"]["subject"], "system:serviceaccount:shop:orders")
        self.assertEqual(tv["jfrog_issuer"], K8S_ISS)
        self.assertEqual(tv["kube_context"], "admin@lab")

    def test_dedicated_sa(self):
        f = self.files(self.render(openshift_cfg(dedicated=True)))
        self.assertEqual(f["terraform.tfvars.json"]["dedicated_service_accounts"],
                         {"orders": {"name": "jfrog-orders", "namespace": "shop"}})
        self.assertIn("kubernetes_service_account_v1", f["main.tf.json"]["resource"])

    def test_bad_subject(self):
        c = openshift_cfg()
        c["target"]["discovered"]["mapping_subject"] = "system:serviceaccount:*:x"
        with self.assertRaises(tfgen.TfgenError):
            self.render(c)


@unittest.skipUnless(os.environ.get("CONSOLE_TF_VALIDATE") == "1" and shutil.which("terraform"),
                     "set CONSOLE_TF_VALIDATE=1 (downloads providers) to run terraform validate")
class ValidateOpenShift(TerraformValidateTests):
    def test_every_kind(self):
        pass

    def test_dedicated_identities(self):
        for cfg in (openshift_cfg(dedicated=True), openshift_cfg()):
            self.validate(cfg)



if __name__ == "__main__":
    unittest.main()
