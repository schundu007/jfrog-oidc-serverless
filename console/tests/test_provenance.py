"""Image provenance: the running image, the ADO build-deploy run that built it (by commit tag) and JFrog's record
of the base image. Recorded answers only."""

import json
import tempfile
import unittest
from pathlib import Path

from console import model
from console.shell import Shell
from console.steps import provenance

ADO_TOKEN = "eyJ0eXAiOiJKV1QifQ.ado.sig"
BASE = "https://dev.azure.com/cdo/proj/_apis"


def cfg():
    c = model.default_config("gcp_run_service", name="t")
    c["target"]["gcp"].update(project="demo", region="us-central1", name="svc")
    return c


def repo():
    d = Path(tempfile.mkdtemp())
    (d / "pipelines" / "vars").mkdir(parents=True)
    (d / "pipelines" / "vars" / "gcp-test.yml").write_text("variables:\n  JFROG_DOCKER_REMOTE: docker-remote\n")
    return d


class Jf:
    def call(self, method, path, body=None):
        if path.endswith("list.manifest.json?stats"):
            return 200, {"lastDownloadedBy": "ado-cdoazure-gcp/ado-build", "lastDownloaded": 1793520000000}
        return 404, None


def fixtures(image, builds):
    describe = {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}
    defs = {"value": [{"id": 7, "name": "Cloud Run service - Build and deploy",
                       "process": {"yamlFilename": "/pipelines/serverless/gcp/gcp-cloud-run-service/build-deploy.yml"}}]}
    return [{"argv": provenance.image_argv(cfg()), "stdout": json.dumps(describe)},
            {"argv": ["az", "account", "get-access-token", "--resource", provenance.ADO_RESOURCE, "--query", "accessToken", "-o", "tsv"],
             "stdout": ADO_TOKEN},
            {"http": f"GET {BASE}/build/definitions?includeAllProperties=true&api-version=7.1", "status": 200, "body": json.dumps(defs)},
            {"http": f"GET {BASE}/build/builds?definitions=7&resultFilter=succeeded&$top=25&api-version=7.1", "status": 200,
             "body": json.dumps({"value": builds})}]


class ProvenanceTests(unittest.TestCase):
    def test_image_tag_ties_the_image_to_its_ado_run(self):
        image = "us-central1-docker.pkg.dev/demo/apps/svc:0d44d6946094"
        builds = [{"id": 190, "buildNumber": "20261101.3", "sourceVersion": "ffff" * 10, "finishTime": "2026-11-01T10:00:00Z"},
                  {"id": 177, "buildNumber": "20261101.1", "sourceVersion": "0d44d6946094a500bcc98ec29d1d11de68550776",
                   "finishTime": "2026-11-01T09:20:00Z", "requestedFor": {"displayName": "Chundu"}}]
        lines = []
        sh = Shell(mode="replay", fixtures=fixtures(image, builds), log=lines.append)
        out = provenance.run(cfg(), sh, {"ado": {"organization": "cdo", "project": "proj"}}, repo(), jfrog=Jf())
        self.assertEqual(out["commit"], "0d44d6946094")
        self.assertEqual(out["build"]["id"], 177)
        self.assertTrue(out["build"]["matches_image"])
        self.assertEqual(out["build"]["url"], "https://dev.azure.com/cdo/proj/_build/results?buildId=177")
        self.assertEqual(out["from_jfrog"], ["acme.jfrog.io/docker-remote/library/python:3.12-slim"])
        self.assertEqual(out["jfrog_record"]["last_downloaded_by"], "ado-cdoazure-gcp/ado-build")
        self.assertNotIn(ADO_TOKEN, json.dumps(out) + "\n".join(lines))

    def test_untagged_image_falls_back_to_the_latest_run(self):
        builds = [{"id": 190, "sourceVersion": "abc", "finishTime": "x"}]
        sh = Shell(mode="replay", fixtures=fixtures("gcr.io/demo/svc:latest", builds), log=[].append)
        out = provenance.run(cfg(), sh, {"ado": {"organization": "cdo", "project": "proj"}}, repo())
        self.assertEqual((out["commit"], out["build"]["id"], out["build"]["matches_image"]), ("", 190, False))

    def test_no_delivery_means_no_ado_lookup(self):
        sh = Shell(mode="replay", fixtures=fixtures("x:0d44d6946094", [])[:1], log=[].append)
        out = provenance.run(cfg(), sh, None, repo())
        self.assertNotIn("build", out)
        self.assertEqual(out["commit"], "0d44d6946094")


if __name__ == "__main__":
    unittest.main()
