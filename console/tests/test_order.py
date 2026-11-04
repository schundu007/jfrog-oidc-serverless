"""The step order: nothing starts while its cloud has a pipeline in flight; Prove and Verify
need a successful Onboard."""

import json
import unittest

from console import order
from console.shell import HttpResult, Result, Shell

ACTIVE_WEB = {"id": 224, "name": "Web App - Build and deploy", "cloud": "azure", "step": 4, "state": "inProgress"}


class PathTests(unittest.TestCase):
    def test_cloud_and_step_from_the_yaml_path(self):
        cases = {
            "pipelines/serverless/azure/azure-web-app/stack-setup.yml": ("azure", 3),
            "/pipelines/serverless/azure/azure-web-app/build-deploy.yml": ("azure", 4),
            "pipelines/azure-workload-onboard.yml": ("azure", 5),
            "pipelines/oidc/azure/aks-jfrog-trust/stack-setup.yml": ("azure", 2),
            "pipelines/kubernetes/gcp/gke-cluster/stack-setup.yml": ("gcp", 3),
            "pipelines/gcp-workload-onboard.yml": ("gcp", 5),
            "pipelines/other.yml": (None, None),
        }
        for path, want in cases.items():
            self.assertEqual((order.cloud_of(path), order.step_of(path)), want, path)


class CheckTests(unittest.TestCase):
    def test_onboard_waits_while_the_cloud_has_a_run_in_flight(self):
        with self.assertRaisesRegex(order.OrderError, r"step 4 \(Build and deploy\).*224"):
            order.check(["preflight", "discover", "publish"], "azure", [ACTIVE_WEB], None)

    def test_another_clouds_run_does_not_block(self):
        order.check(["publish"], "gcp", [ACTIVE_WEB], None)

    def test_prove_needs_a_successful_onboard(self):
        with self.assertRaisesRegex(order.OrderError, "step 5.*not been onboarded"):
            order.check(["prove"], "azure", [], None)
        with self.assertRaisesRegex(order.OrderError, "step 5.*221 failed"):
            order.check(["verify"], "azure", [], {"id": 221, "result": "failed"})
        order.check(["prove"], "azure", [], {"id": 225, "result": "succeeded"})

    def test_the_earliest_step_is_named(self):
        onboard = {"id": 230, "name": "Azure - Onboard", "cloud": "azure", "step": 5, "state": "queued"}
        with self.assertRaisesRegex(order.OrderError, "step 4"):
            order.check(["prove"], "azure", [onboard, ACTIVE_WEB], {"result": "succeeded"})

    def test_other_steps_are_not_checked(self):
        order.check(["discover"], "azure", [ACTIVE_WEB], None)


class FakeAdo(Shell):
    def __init__(self, builds, done, token_ok=True):
        super().__init__(mode="replay")
        self.builds, self.done, self.token_ok = builds, done, token_ok

    def _replay_cmd(self, argv):
        return Result(argv, 0 if self.token_ok else 1, "tok\n" if self.token_ok else "", "", 0.0)

    def _replay_http(self, method, url):
        if "/build/definitions" in url:
            body = {"value": [{"id": 7, "process": {"yamlFilename": "/pipelines/serverless/azure/azure-web-app/build-deploy.yml"}},
                              {"id": 9, "process": {"yamlFilename": "pipelines/azure-workload-onboard.yml"}}]}
        elif "definitions=9" in url:
            body = {"value": self.done}
        else:
            body = {"value": self.builds}
        return HttpResult(method, url, 200, json.dumps(body))


class PipelinesTests(unittest.TestCase):
    def test_reads_active_runs_and_the_last_onboard(self):
        p = order.Pipelines(FakeAdo([{"id": 224, "status": "inProgress", "definition": {"id": 7, "name": "Web App - Build and deploy"}}],
                                    [{"id": 221, "status": "completed", "result": "failed", "definition": {"id": 9, "name": "Onboard"}}]),
                            "cdoazure", "cloudifyDevops")
        active = p.active()
        self.assertEqual([(r["id"], r["cloud"], r["step"]) for r in active], [(224, "azure", 4)])
        self.assertEqual(p.last_onboard("azure")["result"], "failed")
        self.assertIsNone(p.last_onboard("gcp"))

    def test_no_sign_in_is_a_refusal(self):
        with self.assertRaisesRegex(order.OrderError, "no Azure DevOps sign-in"):
            order.Pipelines(FakeAdo([], [], token_ok=False), "o", "p").active()

    def test_token_never_in_errors(self):
        try:
            order.Pipelines(FakeAdo([], [], token_ok=False), "o", "p").active()
        except order.OrderError as e:
            self.assertNotIn("tok", str(e).replace("token", ""))


if __name__ == "__main__":
    unittest.main()
