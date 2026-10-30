import unittest

from console import model


def codes(items):
    return {i["code"] for i in items}


class CronTests(unittest.TestCase):
    def test_hourly_ticks(self):
        c = model.Cron("23 * * * *")
        ticks = c.ticks(0, 3 * 3600)
        self.assertEqual(ticks, [23 * 60, 3600 + 23 * 60, 7200 + 23 * 60])

    def test_steps_ranges_lists(self):
        self.assertEqual(model.Cron("*/15 * * * *").interval_seconds(), 900)
        self.assertEqual(model.Cron("0 */6 * * *").interval_seconds(), 6 * 3600)
        self.assertEqual(model.Cron("0,30 9-17 * * *").interval_seconds(), 24 * 3600 - 17 * 3600 - 1800 + 9 * 3600)

    def test_previous_tick(self):
        self.assertEqual(model.Cron("23 * * * *").previous(0), -3600 + 23 * 60)

    def test_rejects_day_fields_and_garbage(self):
        for bad in ("23 * * * 1", "61 * * * *", "x * * * *", "* * *"):
            with self.assertRaises(ValueError, msg=bad):
                model.Cron(bad)


class DefaultsTests(unittest.TestCase):
    def test_every_kind_has_valid_defaults(self):
        for kind in model.KINDS:
            cfg = model.default_config(kind, name="t")
            res = model.validate(cfg)
            self.assertEqual(res["errors"], [], kind)

    def test_flex_has_no_pull_path(self):
        self.assertFalse(model.default_config("azure_functions_flex")["pull_path"]["enabled"])


class ValidateTests(unittest.TestCase):
    def cfg(self, kind="azure_container_app", **dotted):
        cfg = model.default_config(kind, name="orders-prod")
        for k, v in dotted.items():
            model.set_path(cfg, k.replace("__", "."), v)
        return cfg

    def test_errors(self):
        res = model.validate(self.cfg(**{"jfrog__url": "http://acme.jfrog.io", "target__kind": "aws_lambda"}))
        self.assertIn("jfrog_url_https", codes(res["errors"]))
        self.assertIn("unknown_kind", codes(res["errors"]))
        res = model.validate(self.cfg(**{"name": "Bad Name!", "pull_path__rotation_cron": "nope"}))
        self.assertIn("name", codes(res["errors"]))
        self.assertIn("cron", codes(res["errors"]))

    def test_ttl_math_aca(self):
        cfg = self.cfg()
        cfg["pull_path"]["rotator_image"] = "platformacr.azurecr.io/jfrog-oidc-rotator:0.1.0"
        res = model.validate(cfg)
        m = res["ttl_math"]
        self.assertEqual(m["rotation_interval_seconds"], 3600)
        self.assertEqual(m["pickup_lag_seconds"], 1800)
        self.assertEqual(m["survives_missed_rotations"], 2)   # 3 misses fail (see rotator-misses-3-runs)
        self.assertEqual(m["min_safe_ttl_seconds"], 2 * 3600 + 1800)
        self.assertEqual(res["warnings"], [])

    def test_ttl_math_gcp_no_lag(self):
        m = model.validate(self.cfg("gcp_run_service"))["ttl_math"]
        self.assertEqual(m["pickup_lag_seconds"], 0)
        self.assertEqual(m["survives_missed_rotations"], 2)

    def test_pull_ttl_too_short_for_rotation(self):
        res = model.validate(self.cfg(pull_path__ttl_seconds=7200))
        self.assertIn("pull_ttl_vs_rotation", codes(res["warnings"]))

    def test_pull_ttl_below_min_ttl(self):
        res = model.validate(self.cfg(pull_path__ttl_seconds=1200))
        self.assertIn("pull_ttl_below_min_ttl", codes(res["warnings"]))
        self.assertNotIn("pull_ttl_below_min_ttl", codes(model.validate(self.cfg(pull_path__ttl_seconds=3600))["warnings"]))

    def test_min_ttl_default_matches_rotator_and_modules(self):
        self.assertEqual(model.default_config("azure_container_app")["pull_path"]["min_ttl_seconds"], 1800)

    def test_premium_without_refresh_lag_is_24h(self):
        res = model.validate(self.cfg("azure_functions_premium", pull_path__function_app_refresh=False))
        self.assertEqual(res["ttl_math"]["pickup_lag_seconds"], 86400)
        self.assertIn("pull_ttl_vs_rotation", codes(res["warnings"]))

    def test_runtime_ttl_bounds(self):
        self.assertIn("runtime_ttl_short", codes(model.validate(self.cfg(runtime__ttl_seconds=600))["warnings"]))
        # Runtime tokens are capped at 1 h (AKS pilot mapping rule); the client refreshes before expiry.
        self.assertIn("runtime_ttl_range", codes(model.validate(self.cfg(runtime__ttl_seconds=3601))["errors"]))
        self.assertEqual(model.validate(self.cfg(runtime__ttl_seconds=3600))["errors"], [])
        self.assertIn("runtime_ttl_range", codes(model.validate(self.cfg(runtime__ttl_seconds=100))["errors"]))

    def test_jfrog_url_must_be_origin(self):
        for bad in ("https://acme.jfrog.io/artifactory", "https://acme.jfrog.io:8443", "https://u:p@acme.jfrog.io"):
            with self.subTest(url=bad):
                self.assertIn("jfrog_url_origin", codes(model.validate(self.cfg(jfrog__url=bad))["errors"]))
        self.assertNotIn("jfrog_url_origin", codes(model.validate(self.cfg(jfrog__url="https://acme.jfrog.io/"))["errors"]))

    def test_docker_host(self):
        cfg = model.default_config("azure_container_app")
        self.assertEqual(model.docker_host(cfg), "acme-docker-virtual.jfrog.io")
        cfg["jfrog"].update(url="https://example.jfrog.io", docker_repo="aks-sandbox-test-docker-local")
        self.assertEqual(model.docker_host(cfg), "example-aks-sandbox-test-docker-local.jfrog.io")
        cfg["jfrog"]["docker_host"] = "Registry.Example.com"
        self.assertEqual(model.docker_host(cfg), "registry.example.com")

    def test_docker_host_checks(self):
        self.assertIn("docker_host_bare", codes(model.validate(self.cfg(jfrog__docker_host="https://acme-docker.jfrog.io"))["errors"]))
        self.assertIn("docker_host_bare", codes(model.validate(self.cfg(jfrog__docker_host="acme.jfrog.io/docker"))["errors"]))
        # The platform host means repository-path pulls, which failed on the AKS pilot.
        self.assertIn("docker_host_is_platform", codes(model.validate(self.cfg(jfrog__docker_host="acme.jfrog.io"))["warnings"]))

    def test_system_assigned_identity_warns(self):
        res = model.validate(self.cfg(target__azure__identity_type="system_assigned"))
        self.assertIn("system_assigned_identity", codes(res["warnings"]))

    def test_alert_after_must_beat_ttl(self):
        res = model.validate(self.cfg(pull_path__alert_after_seconds=14400))
        self.assertIn("alert_after_vs_ttl", codes(res["warnings"]))

    def test_schema_lists_kinds_and_defaults(self):
        s = model.schema()
        self.assertEqual(set(s["kinds"]), set(model.KINDS))
        self.assertIn("azure_container_app", s["defaults"])


if __name__ == "__main__":
    unittest.main()


class RotatorImageTests(unittest.TestCase):
    def test_missing_rotator_image_warns_only_with_pull_path(self):
        cfg = model.default_config("azure_container_app", name="t")
        self.assertIn("rotator_image_missing", [w["code"] for w in model.validate(cfg)["warnings"]])
        cfg["pull_path"]["enabled"] = False
        self.assertNotIn("rotator_image_missing", [w["code"] for w in model.validate(cfg)["warnings"]])
