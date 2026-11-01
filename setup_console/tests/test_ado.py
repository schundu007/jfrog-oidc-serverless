"""setup_console/ado.py: queue a stack's ADO pipeline, read its plan, answer its approval. No network."""

import json
import unittest

from console.shell import HttpResult, Result, Shell
from setup_console import ado

TOKEN = "eyJ0eXAiOiJKV1QifQ.eyJzdWIiOiJ4In0.c2ln"   # never logged or returned

PLAN_LOG = """2026-10-31T21:00:00.0000000Z ##[section]Starting: terraform apply plan (OIDC)
2026-10-31T21:00:01.0000000Z   # google_service_account.workload will be created
2026-10-31T21:00:01.0000000Z   # google_cloud_run_v2_job.workload[0] must be replaced
2026-10-31T21:00:01.0000000Z   # google_storage_bucket.old will be destroyed
2026-10-31T21:00:01.0000000Z   # google_project_iam_member.x will be updated in-place
2026-10-31T21:00:02.0000000Z Plan: 2 to add, 1 to change, 2 to destroy.
"""
APPLY_LOG = """2026-10-31T21:05:00.0000000Z Apply complete! Resources: 2 added, 0 changed, 0 destroyed.
2026-10-31T21:05:00.0000000Z job_name = "artifact-reader"
2026-10-31T21:05:00.0000000Z workload_service_account = "artifact-reader@p.iam.gserviceaccount.com"
2026-10-31T21:05:00.0000000Z ##[section]Finishing: terraform apply (OIDC)
"""


def timeline(plan=("completed", "succeeded"), apply=("inProgress", None), issues=(), approval=None):
    recs = ([{"type": "Checkpoint.Approval", "id": approval, "state": "inProgress"}] if approval else []) + [{"type": "Stage", "identifier": "plan", "state": plan[0], "result": plan[1]},
            {"type": "Stage", "identifier": "apply", "state": apply[0], "result": apply[1]},
            {"type": "Task", "name": "terraform apply plan (OIDC)", "state": "completed", "result": plan[1],
             "log": {"id": 9}, "issues": [{"type": "error", "message": m} for m in issues]},
            {"type": "Task", "name": "terraform apply (OIDC)", "state": "completed", "result": "succeeded", "log": {"id": 21}}]
    return json.dumps({"records": recs})


class FakeAdo(Shell):
    def __init__(self, routes):
        super().__init__(mode="replay", log=self.lines_append)
        self.lines, self.routes, self.sent = [], routes, []

    def lines_append(self, line):
        self.lines.append(line)

    def _replay_cmd(self, argv):
        if argv[:3] == ["az", "account", "get-access-token"]:
            return Result(argv, 0, TOKEN + "\n", "", 0.0)
        return Result(argv, 127, "", "no fixture", 0.0)

    def http(self, method, url, headers=None, body=None, timeout=20.0, sensitive=False):
        self.sent.append((method, url, headers or {}, json.loads(body) if body else None))
        for (m, frag), (status, text) in self.routes.items():
            if m == method and frag in url:
                return HttpResult(method, url, status, text if isinstance(text, str) else json.dumps(text))
        return HttpResult(method, url, 404, "{}")


class AdoTests(unittest.TestCase):
    def client(self, routes):
        sh = FakeAdo(routes)
        return ado.Ado(sh, "acme", "proj"), sh

    def test_finds_a_pipeline_by_its_yaml_path(self):
        c, _ = self.client({("GET", "/build/definitions?"): (200, {"value": [
            {"id": 27, "name": "Cloud Run job - Infrastructure", "process": {"yamlFilename": "pipelines/serverless/gcp/gcp-cloud-run/stack-setup.yml"}},
            {"id": 26, "name": "Cloud Run job - Build and deploy", "process": {"yamlFilename": "pipelines/serverless/gcp/gcp-cloud-run/build-deploy.yml"}}]})})
        self.assertEqual(c.pipeline("pipelines/serverless/gcp/gcp-cloud-run/stack-setup.yml"), (27, "Cloud Run job - Infrastructure"))
        with self.assertRaises(ado.AdoError):
            c.pipeline("pipelines/serverless/gcp/nope/stack-setup.yml")

    def test_queue_sends_object_parameters_as_json_and_the_token_only_as_a_header(self):
        c, sh = self.client({("POST", "/pipelines/27/runs"): (200, {"id": 101, "state": "inProgress"})})
        run = c.queue(27, {"action": "apply", "inputs": {"deploy_job": False}})
        self.assertEqual(run, 101)
        method, url, headers, body = sh.sent[-1]
        self.assertEqual(body["templateParameters"], {"action": "apply", "inputs": '{"deploy_job": false}'})
        self.assertEqual(body["resources"]["repositories"]["self"]["refName"], "refs/heads/main")
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, url)
        self.assertFalse(any(TOKEN in l for l in sh.lines))

    def test_plan_reads_the_changes_and_the_pending_approval(self):
        # ADO sends a log asked for as JSON as {"count", "value": [lines]}.
        c, _ = self.client({("GET", "/builds/101/timeline"): (200, timeline(apply=("pending", None), approval="ap-1")),
                            ("GET", "/builds/101/logs/9"): (200, {"count": 6, "value": PLAN_LOG.splitlines()})})
        st = c.stack_run(101)
        self.assertEqual(st["phase"], "waiting_approval")
        self.assertEqual(st["approval"], "ap-1")
        self.assertEqual(st["summary"], {"create": 1, "replace": 1, "delete": 1, "update": 1})
        self.assertEqual([(x["action"], x["address"]) for x in st["changes"]],
                         [("create", "google_service_account.workload"), ("replace", "google_cloud_run_v2_job.workload[0]"),
                          ("delete", "google_storage_bucket.old"), ("update", "google_project_iam_member.x")])
        self.assertEqual(st["destructive"], ["google_cloud_run_v2_job.workload[0]", "google_storage_bucket.old"])
        self.assertEqual(len(st["sha256"]), 64)

    def test_stream_shows_each_steps_new_output_once_without_commands(self):
        recs = [{"type": "Task", "name": "terraform apply plan (OIDC)", "state": "inProgress", "log": {"id": 9},
                 "startTime": "2026-10-31T21:00:00Z"},
                {"type": "Task", "name": "skipped", "state": "completed", "result": "skipped", "log": {"id": 3}}]
        first = ["2026-10-31T21:00:00.0Z ##[section]Starting: terraform apply plan (OIDC)",
                 "2026-10-31T21:00:00.1Z [command]/usr/bin/bash pipelines/scripts/terraform-stack.sh",
                 "2026-10-31T21:00:01.0Z Initializing the backend...",
                 f"2026-10-31T21:00:01.1Z token={TOKEN}"]
        routes = {("GET", "/builds/7/timeline"): (200, {"records": recs}),
                  ("GET", "/builds/7/logs/9?startLine=5"): (200, {"value": ["2026-10-31T21:00:02.0Z Plan: 1 to add."]}),
                  ("GET", "/builds/7/logs/9"): (200, {"value": first})}
        c, _ = self.client(routes)
        seen = {}
        out = c.stream(7, seen)
        self.assertEqual(out[:2], ["── terraform apply plan (OIDC)", "Initializing the backend..."])
        self.assertFalse(any("[command]" in l or "bash" in l for l in out))
        self.assertFalse(any(TOKEN in l for l in out))
        recs[0]["state"] = "completed"
        self.assertEqual(c.stream(7, seen), ["Plan: 1 to add."])
        self.assertEqual(c.stream(7, seen), [])   # read to its end

    def test_an_unpermitted_environment_is_named(self):
        recs = json.loads(timeline(apply=("pending", None)))["records"] + [{"type": "Checkpoint.Authorization", "state": "inProgress"}]
        c, _ = self.client({("GET", "/builds/5/timeline"): (200, {"records": recs}),
                            ("GET", "/builds/5/logs/9"): (200, PLAN_LOG)})
        st = c.stack_run(5)
        self.assertEqual(st["phase"], "authorizing")
        self.assertIn("foundation", st["error"])

    def test_no_changes_ends_without_an_approval(self):
        c, _ = self.client({("GET", "/builds/7/timeline"): (200, timeline(apply=("completed", "skipped"))),
                            ("GET", "/builds/7/logs/9"): (200, "2026-10-31T21:00:00.0Z No changes. Your infrastructure matches the configuration.\n"),
                            ("GET", "/builds/7?"): (200, {"status": "completed", "result": "succeeded"})})
        st = c.stack_run(7)
        self.assertEqual((st["phase"], st["summary"]), ("done", {}))

    def test_a_failed_plan_says_why(self):
        c, _ = self.client({("GET", "/builds/8/timeline"): (200, timeline(plan=("completed", "failed"), apply=("completed", "skipped"),
                                                                            issues=["Bash exited with code '1'."])),
                            ("GET", "/builds/8/logs/9"): (200, "2026-10-31T21:00:00.0Z Error: Invalid value for input variable\n"),
                            ("GET", "/builds/8?"): (200, {"status": "completed", "result": "failed"})})
        st = c.stack_run(8)
        self.assertEqual(st["phase"], "failed")
        self.assertIn("Invalid value for input variable", st["error"])

    def test_applied_run_gives_its_outputs(self):
        c, _ = self.client({("GET", "/builds/101/timeline"): (200, timeline(apply=("completed", "succeeded"))),
                            ("GET", "/builds/101/logs/9"): (200, PLAN_LOG),
                            ("GET", "/builds/101/logs/21"): (200, APPLY_LOG),
                            ("GET", "/builds/101?"): (200, {"status": "completed", "result": "succeeded"})})
        st = c.stack_run(101)
        self.assertEqual(st["phase"], "done")
        self.assertEqual(st["outputs"], {"job_name": "artifact-reader", "workload_service_account": "artifact-reader@p.iam.gserviceaccount.com"})

    def test_answer_approves_or_rejects(self):
        c, sh = self.client({("PATCH", "/pipelines/approvals"): (200, {"value": [{"id": "ap-1", "status": "approved"}]})})
        c.answer("ap-1", True, "plan abc approved in the console")
        self.assertEqual(sh.sent[-1][3], [{"approvalId": "ap-1", "status": "approved", "comment": "plan abc approved in the console"}])
        c.answer("ap-1", False, "declined")
        self.assertEqual(sh.sent[-1][3][0]["status"], "rejected")

    def test_build_warnings_name_the_first_image(self):
        recs = {"records": [{"type": "Task", "name": "Roll out artifact-reader (OIDC)", "state": "completed", "result": "succeeded",
                             "issues": [{"type": "warning", "message": "Cloud Run job artifact-reader does not exist yet: pushed "
                                         "us-central1-docker.pkg.dev/p/platform/artifact-reader:abc. In Setup, set job_image to it"}]}]}
        c, _ = self.client({("GET", "/builds/100/timeline"): (200, recs)})
        self.assertEqual(c.warnings(100), ["Cloud Run job artifact-reader does not exist yet: pushed "
                                           "us-central1-docker.pkg.dev/p/platform/artifact-reader:abc. In Setup, set job_image to it"])


if __name__ == "__main__":
    unittest.main()
