"""Inventory for AWS profiles and kube contexts.

The AWS Lambda AccessDenied line and the empty ECS / App Runner listings were
recorded live on a test account (2026-10-30, aws-cli 2.34.55); the populated
listings and the ksvc list are doc-derived.
"""

import json
import unittest

from console import inventory
from console.shell import Result, Shell

A = "123456789012"
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


AWS_OUT = {
    ("aws", "configure", "list-profiles"): "default\nschundu\n",
    ("aws", "configure", "get", "region"): "us-east-1\n",
    ("aws", "sts", "get-caller-identity"): {"Account": A, "Arn": f"arn:aws:iam::{A}:root"},
    ("aws", "lambda", "list-functions"): ["orders-api", "billing"],
    ("aws", "ecs", "list-clusters"): {"clusterArns": [f"arn:aws:ecs:us-east-1:{A}:cluster/apps"]},
    ("aws", "ecs", "list-services"): {"serviceArns": [f"arn:aws:ecs:us-east-1:{A}:service/apps/orders-svc"]},
    ("aws", "ecs", "list-task-definition-families"): {"families": ["orders-svc", "nightly-export"]},
    ("aws", "apprunner", "list-services"): {"ServiceSummaryList": [{"ServiceName": "orders-web"}]},
}


class AwsInventoryTests(unittest.TestCase):
    def test_lists_every_kind(self):
        sh = Fake(AWS_OUT)
        res = inventory.run("aws", sh, None, scope="schundu")
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["defaults"], {"profile": "schundu", "account": A, "region": "us-east-1",
                                           "jfrog_url": ""})
        got = {(w["kind"], w["name"], w["cluster"]) for w in res["workloads"]}
        self.assertEqual(got, {("aws_lambda", "orders-api", ""), ("aws_lambda", "billing", ""),
                               ("aws_ecs_task", "orders-svc", "apps"), ("aws_ecs_task", "nightly-export", ""),
                               ("aws_app_runner", "orders-web", "")})
        self.assertEqual([s["id"] for s in res["scopes"]], ["default", "schundu"])
        self.assertTrue(all("--profile" in c and "schundu" in c for c in sh.calls if c[1] not in ("configure",)))

    def test_recorded_lambda_access_denied_is_one_line_and_the_rest_lists(self):
        empty = {**AWS_OUT, ("aws", "ecs", "list-clusters"): {"clusterArns": []},
                 ("aws", "ecs", "list-task-definition-families"): {"families": []},
                 ("aws", "apprunner", "list-services"): {"ServiceSummaryList": []}}
        sh = Fake(empty, fail={("aws", "lambda", "list-functions"):
                               "\naws: [ERROR]: An error occurred (AccessDeniedException) when calling the "
                               "ListFunctions operation: None\n"})
        res = inventory.run("aws", sh, None)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("AccessDeniedException", res["errors"][0])
        self.assertEqual(res["workloads"], [])

    def test_not_logged_in(self):
        sh = Fake(AWS_OUT, fail={("aws", "sts", "get-caller-identity"): "Token has expired and refresh failed"})
        res = inventory.run("aws", sh, None)
        self.assertIn("aws sso login", res["errors"][0])

    def test_onboarded_mark(self):
        cfg = {"target": {"kind": "aws_lambda", "aws": {"account": A, "region": "us-east-1", "name": "billing"}}}
        res = inventory.run("aws", Fake(AWS_OUT), None, configs=[cfg])
        marks = {w["name"]: w["onboarded"] for w in res["workloads"]}
        self.assertTrue(marks["billing"])
        self.assertFalse(marks["orders-api"])

    def test_only_read_commands(self):
        sh = Fake(AWS_OUT)
        inventory.run("aws", sh, None)
        writes = {"create", "update", "delete", "set", "put", "register", "start", "run", "enable"}
        self.assertFalse([c for c in sh.calls if writes & {w for a in c for w in a.split("-")}])

    def test_bad_profile_refused(self):
        sh = Fake(AWS_OUT)
        res = inventory.run("aws", sh, None, scope="bad profile;rm")
        self.assertEqual(sh.calls, [])
        self.assertIn("AWS CLI profile", res["errors"][0])


class KnativeInventoryTests(unittest.TestCase):
    OUT = {("kubectl", "config", "get-contexts"): f"{CTX}\nkind-dev\n",
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

    def test_cluster_slug(self):
        self.assertEqual(inventory.cluster_slug("kind-dev"), "kind-dev")
        self.assertEqual(inventory.cluster_slug("default/api-ocp-acme-io:6443/admin"), "ocp-acme-io")
        self.assertTrue(inventory.cluster_slug("123")[0].isalpha())


if __name__ == "__main__":
    unittest.main()
