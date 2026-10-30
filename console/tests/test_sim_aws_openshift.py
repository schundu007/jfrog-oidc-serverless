"""The simulator for AWS and Knative targets runs the real client against the
AWS (STS + container credentials) and Kubernetes (projected token) emulators."""

import json
import unittest

from console.sim import scenario as sc
from console.sim.engine import Simulation


def run(name, **kw):
    sim = Simulation(sc.load(name, **kw))
    return sim, sim.run()


class AwsSimTests(unittest.TestCase):
    def test_lambda_signs_and_exchanges(self):
        sim, res = run("happy-aws-lambda")
        self.assertEqual(res["verdict"], "pass")
        self.assertGreater(sim.aws.issued, 0)               # real SigV4 calls verified by the emulator
        self.assertGreater(res["summary"]["tokens_minted"], 0)
        self.assertEqual(res["summary"]["requests_failed"], 0)
        self.assertIsNone(sim.platform)                      # runtime path only

    def test_container_credentials_path(self):
        sim, res = run("happy-aws-ecs")
        self.assertEqual(res["verdict"], "pass")
        self.assertIn("AWS_CONTAINER_CREDENTIALS_FULL_URI", sim.runtime_env)
        self.assertGreater(sim.aws.issued, 0)

    def test_bad_secret_is_rejected_by_sigv4_check(self):
        sim = Simulation(sc.load("happy-aws-lambda", duration="1h"))
        orig = sim._build_aws

        def tampered(*a):
            out = orig(*a)
            sim.runtime_env["AWS_SECRET_ACCESS_KEY"] = "wrong"
            return out
        sim._build_aws = tampered
        res = sim.run()
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("SignatureDoesNotMatch", " ".join(res["chain"]))

    def test_federation_disabled_root_cause(self):
        _, res = run("aws-federation-disabled", duration="2h")
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("aws iam enable-outbound-web-identity-federation --region us-east-1", res["root_cause"])

    def test_no_secret_in_result(self):
        sim, res = run("happy-aws-lambda", duration="1h")
        blob = json.dumps(res)
        for k in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN"):
            if k in sim.runtime_env:
                self.assertNotIn(sim.runtime_env[k], blob)

    def test_fault_cloud_check(self):
        with self.assertRaises(ValueError):
            sc.load("happy-aws-lambda", faults=[{"type": "rotator_down", "start": "01:00"}])
        with self.assertRaises(ValueError):
            sc.load("happy-openshift-knative", faults=[{"type": "federation_disabled", "start": "01:00"}])


class OpenShiftSimTests(unittest.TestCase):
    def test_kubelet_refreshes_token_file(self):
        sim, res = run("happy-openshift-knative")
        self.assertEqual(res["verdict"], "pass")
        # 24 h with a 1 h token refreshed at 80%: about 30 writes.
        self.assertGreaterEqual(sim.k8s.refreshes, 25)
        self.assertGreater(res["summary"]["tokens_minted"], 0)

    def test_sa_recreated_same_name_still_matches(self):
        _, res = run("happy-openshift-knative", faults=[{"type": "identity_recreated", "start": "03:00"}])
        self.assertEqual(res["verdict"], "pass")

    def test_mapping_revoked_fails(self):
        _, res = run("happy-openshift-knative", faults=[{"type": "mapping_revoked", "start": "03:00"}])
        self.assertEqual(res["verdict"], "fail")


if __name__ == "__main__":
    unittest.main()
