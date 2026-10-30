"""Diagnosis rules fed with events seen on a real cluster."""

import unittest

from kcp_console.diagnose import diagnose

# kubelet events from kcp-test-aks (2026-10-30): the exchange worked, the remote repo refused to cache.
REMOTE_CACHE_403 = """Failed  Failed to pull image "example.jfrog.io/docker-remote/library/busybox:1.36": failed to copy: httpReadSeeker: failed open: unexpected status from GET request to https://example.jfrog.io/v2/docker-remote/library/busybox/manifests/sha256:b7f3: 403 Forbidden
unknown: Rejected artifact download request: User kcp-pull-user is not permitted to deploy 'library/busybox/sha256__b7f3/manifest.json' into 'docker-remote-cache:library/busybox/sha256__b7f3/manifest.json'.
Failed  Error: ErrImagePull"""


class RemoteCache403Test(unittest.TestCase):
    def test_names_deploy_cache_on_the_remote(self):
        r = diagnose({"pods": [{"kind": "test", "name": "secretless-pull-test", "phase": "Pending", "events": REMOTE_CACHE_403}]})
        f = next(x for x in r["findings"] if x["rule"] == "pull-403")
        self.assertIn("Deploy/Cache", f["title"])
        self.assertIn("docker-remote", f["title"])
        self.assertIn("kcp-pull-user", f["title"])


if __name__ == "__main__":
    unittest.main()
