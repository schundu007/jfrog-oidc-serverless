"""M1 acceptance: every built-in scenario meets its expectation, a 24 h scenario
runs in under 10 s, and the faults that break the runtime exchange fail.

These run the real jfrog_oidc client against the emulators.
"""

import json
import time
import unittest

from console.sim import scenario as sc
from console.sim.engine import Simulation

_results: dict = {}


def run(name, **kw):
    """Run a built-in once per test session (scenarios are deterministic per seed)."""
    key = (name, json.dumps(kw, sort_keys=True))
    if key not in _results:
        _results[key] = Simulation(sc.load(name, **kw)).run()
    return _results[key]


class BuiltInScenarioTests(unittest.TestCase):
    def test_all_builtins_listed(self):
        names = set(sc.builtin_names())
        self.assertEqual(names, {
            "happy-gcp-function", "happy-gcp-worker-pool", "happy-aca", "happy-functions-premium", "happy-functions-flex", "jfrog-outage-90m",
            "azure-v1-tokens", "wrong-audience", "sa-recreated",
            "happy-openshift-knative", "openshift-wrong-audience",
        })

    def test_every_builtin_meets_expectation(self):
        for name in sc.builtin_names():
            with self.subTest(scenario=name):
                scen = sc.load(name)
                result = run(name)
                problems = sc.check(result, scen["expect"])
                self.assertEqual(problems, [], f"{name}: {result['verdict']} chain={result['chain']}")


class FidelityTests(unittest.TestCase):
    def test_24h_scenario_under_10s(self):
        started = time.perf_counter()
        result = Simulation(sc.load("happy-aca", seed=99)).run()
        elapsed = time.perf_counter() - started
        self.assertEqual(result["duration_s"], 86400)
        self.assertLess(elapsed, 10.0)

    def test_runs_the_real_client(self):
        import jfrog_oidc

        result = run("happy-gcp-function")
        self.assertIn("client/src/jfrog_oidc", jfrog_oidc.__file__.replace("\\", "/"))
        self.assertGreater(result["summary"]["emulator_requests"], 1000)

    def test_deterministic_for_a_seed(self):
        a = Simulation(sc.load("jfrog-outage-90m")).run()
        b = Simulation(sc.load("jfrog-outage-90m")).run()
        self.assertEqual(a["verdict"], b["verdict"])
        self.assertEqual({k: v for k, v in a["summary"].items() if k != "elapsed_s"},
                         {k: v for k, v in b["summary"].items() if k != "elapsed_s"})
        self.assertEqual(a["chain"], b["chain"])

    def test_outage_serves_cached_runtime_tokens(self):
        r = run("jfrog-outage-90m")
        self.assertEqual(r["verdict"], "degraded")
        self.assertGreater(r["summary"]["requests_ok_cached"], 0)
        self.assertGreater(r["summary"]["requests_unavailable"] + r["summary"]["requests_failed"], 0)

    def test_output_has_no_tokens(self):
        for name in ("happy-aca", "happy-gcp-function", "azure-v1-tokens"):
            blob = json.dumps(run(name))
            self.assertNotIn("eyJ", blob, name)
            self.assertNotIn("gcp-at-", blob, name)
            self.assertNotIn("az-at-", blob, name)

    def test_saved_config_as_scenario(self):
        from console import model
        cfg = model.default_config("azure_container_app", name="orders-prod")
        scen = sc.from_config(cfg)
        self.assertEqual(scen["faults"], [])
        self.assertEqual(scen["target_kind"], "azure_container_app")
        self.assertEqual(Simulation(scen).run()["verdict"], "pass")

    def test_scenario_validation(self):
        with self.assertRaises(ValueError):
            sc.normalize({"name": "x", "target_kind": "aws"})
        with self.assertRaises(ValueError):
            sc.normalize({"name": "x", "target_kind": "azure_container_app", "faults": [{"type": "meteor"}]})
        with self.assertRaises(ValueError):
            sc.load("no-such-scenario")


if __name__ == "__main__":
    unittest.main()
