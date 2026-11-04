import unittest

from console import model


def codes(items):
    return {i["code"] for i in items}


class DefaultsTests(unittest.TestCase):
    def test_every_kind_has_valid_defaults(self):
        for kind in model.KINDS:
            cfg = model.default_config(kind, name="t")
            res = model.validate(cfg)
            self.assertEqual(res["errors"], [], kind)

    def test_flex_is_a_kind(self):
        self.assertEqual(model.cloud_of("azure_functions_flex"), "azure")

    def test_azure_is_runtime_only(self):
        for kind in [k for k in model.KINDS if model.cloud_of(k) == "azure"]:
            cfg = model.default_config(kind, name="orders")
            self.assertNotIn("pull_path", cfg, kind)
            self.assertEqual(model.validate(cfg)["errors"], [], kind)


class OpenShiftModelTests(unittest.TestCase):
    def test_defaults_are_runtime_only(self):
        for kind in ("openshift_knative_service",):
            cfg = model.default_config(kind, name="orders")
            self.assertNotIn("pull_path", cfg, kind)
            self.assertEqual(model.validate(cfg)["errors"], [], kind)
            self.assertEqual(model.dedicated_identity_name(cfg), "jfrog-orders")

    def test_openshift_fields(self):
        cfg = model.default_config("openshift_knative_service", name="orders")
        cfg["target"]["openshift"].update(cluster="Bad_Cluster", namespace="")
        errs = model.validate(cfg)["errors"]
        self.assertIn("cluster_name", codes(errs))
        self.assertIn("target.openshift.namespace", {e["path"] for e in errs})


class ValidateTests(unittest.TestCase):
    def cfg(self, kind="gcp_run_service", **dotted):
        cfg = model.default_config(kind, name="orders-prod")
        for k, v in dotted.items():
            model.set_path(cfg, k.replace("__", "."), v)
        return cfg

    def test_errors(self):
        res = model.validate(self.cfg(**{"jfrog__url": "http://acme.jfrog.io", "target__kind": "oci_functions"}))
        self.assertIn("jfrog_url_https", codes(res["errors"]))
        self.assertIn("unknown_kind", codes(res["errors"]))
        res = model.validate(self.cfg(**{"name": "Bad Name!"}))
        self.assertIn("name", codes(res["errors"]))
        self.assertNotIn("ttl_math", res)

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
        res = model.validate(self.cfg("azure_container_app", target__azure__identity_type="system_assigned"))
        self.assertIn("system_assigned_identity", codes(res["warnings"]))

    def test_schema_lists_kinds_and_defaults(self):
        s = model.schema()
        self.assertEqual(set(s["kinds"]), set(model.KINDS))
        self.assertIn("azure_container_app", s["defaults"])


if __name__ == "__main__":
    unittest.main()


class VerifyPathTests(unittest.TestCase):
    def test_azure_reads_the_file_its_jfrog_trust_stack_seeds(self):
        self.assertEqual(model.default_config("azure_web_app", name="w")["verify"]["artifact_path"],
                         "azure-generic-local/hello.txt")
        self.assertEqual(model.default_config("gcp_run_service", name="w")["verify"]["artifact_path"],
                         "generic-local/healthcheck/ping.txt")
