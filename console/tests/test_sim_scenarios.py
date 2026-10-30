"""M1 acceptance: every built-in scenario meets its expectation, a 24 h scenario
runs in under 10 s, and lowering a mapping TTL flips a happy scenario to fail.

These run the real jfrog_oidc client and rotator/rotate.py against the emulators.
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
            "happy-gcp-function", "happy-aca", "happy-functions-premium", "jfrog-outage-90m",
            "rotator-misses-2-runs", "rotator-misses-3-runs", "rotator-down-5h", "ttl-too-short",
            "premium-no-refresh-permission", "azure-v1-tokens", "wrong-audience", "sa-recreated", "min-ttl-refusal",
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

    def test_mutation_lower_pull_ttl_flips_happy_to_fail(self):
        for name in ("happy-aca", "happy-gcp-function", "happy-functions-premium"):
            with self.subTest(scenario=name):
                # 1800 s passes MIN_TTL_SECONDS (1800) but expires 30 min before the next hourly rotation.
                result = run(name, overrides={"pull_path.ttl_seconds": 1800})
                self.assertEqual(result["verdict"], "fail")
                self.assertIn("pull TTL", result["root_cause"] or "")

    def test_runs_the_real_rotator_and_client(self):
        import rotate
        import jfrog_oidc

        result = run("happy-aca")
        # Rotator JSON log lines (rotate.log) are what the engine records.
        rotator_events = [e for e in result["events"] if e["type"] == "rotator"]
        self.assertTrue(any("rotation_succeeded" in e["message"] for e in rotator_events))
        self.assertIn("rotator/rotate.py", rotate.__file__.replace("\\", "/"))
        self.assertIn("client/src/jfrog_oidc", jfrog_oidc.__file__.replace("\\", "/"))
        self.assertGreater(result["summary"]["emulator_requests"], 1000)

    def test_deterministic_for_a_seed(self):
        a = Simulation(sc.load("rotator-misses-3-runs")).run()
        b = Simulation(sc.load("rotator-misses-3-runs")).run()
        self.assertEqual(a["verdict"], b["verdict"])
        self.assertEqual(a["summary"]["pulls_failed"], b["summary"]["pulls_failed"])
        self.assertEqual(a["chain"], b["chain"])

    def test_causal_chain_for_missed_rotations(self):
        r = run("rotator-misses-3-runs")
        text = " → ".join(r["chain"])
        self.assertIn("rotations 03:23, 04:23, 05:23 failed (fault rotator_down)", text)
        self.assertIn("expired 06:23", text)
        # Either a replica start landed in the gap, or none did and the exposure alone fails the run.
        self.assertTrue("registry 401" in text or "held an unusable credential 06:23" in text, text)
        self.assertGreater(r["summary"]["exposure_s"], 300)
        self.assertLess(r["summary"]["alert_first_fired_s"], 6 * 3600 + 23 * 60)

    def test_outage_serves_cached_runtime_tokens(self):
        r = run("jfrog-outage-90m")
        self.assertEqual(r["verdict"], "degraded")
        self.assertGreater(r["summary"]["requests_ok_cached"], 0)
        self.assertGreater(r["summary"]["pulls_failed"], 0)

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
