"""The simulator for Knative targets runs the real client against the Kubernetes (projected token) emulator."""

import json
import unittest

from console.sim import scenario as sc
from console.sim.engine import Simulation


def run(name, **kw):
    sim = Simulation(sc.load(name, **kw))
    return sim, sim.run()


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
