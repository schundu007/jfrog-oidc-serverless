"""Workspaces for AWS accounts and Knative clusters (JSON syntax, constants-only main.tf.json).

    CONSOLE_TF_VALIDATE=1 PYTHONPATH=client/src:. python -m unittest console.tests.test_tfgen_aws_openshift
"""

import json
import os
import shutil
import unittest

from console import model
from console.steps import tfgen
from console.tests.test_tfgen import Base, TerraformValidateTests

A = "123456789012"
ISS = "https://a1d2b0fd-1177-4468-9351-2feeeeeee723.tokens.sts.global.api.aws"
K8S_ISS = "https://oidc.example-cluster.openshiftapps.com"


def aws_cfg(kind="aws_lambda", name="orders", role=None, dedicated=False):
    cfg = model.default_config(kind, name=name)
    cfg["target"]["aws"].update(account=A, region="us-east-1", name=name)
    cfg["jfrog"]["integration_names"]["aws"] = f"aws-{A}"
    d = {"kind": kind, "issuer": ISS, "mapping_subject": None if dedicated else (role or f"arn:aws:iam::{A}:role/{name}-role")}
    if dedicated:
        d["dedicated_identity"] = model.dedicated_identity_name(cfg)
    cfg["target"]["discovered"] = d
    return cfg


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


class AwsWorkspaceTests(Base):
    def test_one_workspace_per_account(self):
        r1 = self.render(aws_cfg("aws_lambda", "orders"))
        r2 = self.render(aws_cfg("aws_ecs_task", "billing"))
        self.assertEqual(r1["key"], f"aws:{A}")
        self.assertEqual(r2["targets"], ["billing", "orders"])
        self.assertTrue(r1["dir"].endswith(f"aws_{A}"))

    def test_mapping_on_role_arn_and_issuer(self):
        f = self.files(self.render(aws_cfg()))
        tv, main = f["terraform.tfvars.json"], f["main.tf.json"]
        self.assertEqual(tv["workloads"]["orders-runtime"]["subject"], f"arn:aws:iam::{A}:role/orders-role")
        self.assertEqual(tv["workloads"]["orders-runtime"]["cloud"], "aws")
        self.assertEqual(tv["jfrog_issuer"], ISS)
        self.assertEqual(tv["existing_roles"], {"orders": "orders-role"})
        self.assertEqual(tv["pull_path"]["enabled"], False)
        self.assertEqual(main["provider"]["aws"]["allowed_account_ids"], ["${var.aws_account_id}"])
        self.assertTrue(main["module"]["aws_identity"]["source"].endswith("terraform/modules/aws-identity"))

    def test_dedicated_role(self):
        f = self.files(self.render(aws_cfg(name="billing", dedicated=True)))
        tv = f["terraform.tfvars.json"]
        self.assertEqual(tv["dedicated_roles"], {"billing": {"name": "jfrog-billing", "kind": "aws_lambda"}})
        self.assertEqual(tv["existing_roles"], {})
        self.assertEqual(tv["workloads"]["billing-runtime"]["subject"], "")
        self.assertEqual(f["main.tf.json"]["locals"]["dedicated_subjects"], "${module.aws_identity.dedicated_role_arns}")

    def test_operator_values_never_in_main(self):
        main = json.dumps(self.files(self.render(aws_cfg()))["main.tf.json"])
        for v in (A, ISS, "orders-role", "us-east-1"):
            self.assertNotIn(v, main)

    def test_refuses_without_issuer_or_with_bad_subject(self):
        c = aws_cfg()
        del c["target"]["discovered"]["issuer"]
        with self.assertRaisesRegex(tfgen.TfgenError, "issuer"):
            self.render(c)
        with self.assertRaisesRegex(tfgen.TfgenError, "not an IAM role ARN"):
            self.render(aws_cfg(role="arn:aws:iam::123456789012:user/bob"))

    def test_same_role_twice_refused(self):
        self.render(aws_cfg(name="orders", role=f"arn:aws:iam::{A}:role/shared"))
        with self.assertRaisesRegex(tfgen.TfgenError, "same identity"):
            self.render(aws_cfg(name="billing", role=f"arn:aws:iam::{A}:role/shared"))

    def test_s3_backend(self):
        c = aws_cfg()
        c["state_backend"] = {"type": "s3", "bucket": "tfstate-123456789012", "key": "workspaces/aws.tfstate",
                              "region": "us-east-1"}
        b = self.files(self.render(c))["backend.tf.json"]["terraform"]["backend"]["s3"]
        self.assertTrue(b["use_lockfile"])


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
class ValidateAwsOpenShift(TerraformValidateTests):
    def test_every_kind(self):
        pass

    def test_dedicated_identities(self):
        for cfg in (aws_cfg(dedicated=True), aws_cfg("aws_app_runner", "web"), openshift_cfg(dedicated=True),
                    openshift_cfg()):
            self.validate(cfg)

    def test_pull_path_off(self):
        pass


if __name__ == "__main__":
    unittest.main()
