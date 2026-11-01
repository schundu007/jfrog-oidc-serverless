"""setup_console/provision.py: a missing workload is deployed through its ADO pipelines after a Yes."""

import shutil
import unittest

from setup_console import provision, stacks
from setup_console.jobs import Jobs
from setup_console.tests.test_setup import PLANNED, Root, fake_shell

IMAGE = "us-central1-docker.pkg.dev/proj-1/platform/artifact-reader:abc123"


class FakeAdo:
    """Stack runs: each queued run plans (PLANNED), then is done once approved. Builds push a first image."""

    def __init__(self):
        self.queued, self.answers, self.runs = [], [], {}

    def pipeline(self, path):
        return (26, "build") if path.endswith("build-deploy.yml") else (27, "stack")

    def queue(self, pid, params):
        run = 100 + len(self.queued)
        self.queued.append((pid, params))
        self.runs[run] = "planned"
        return run

    def run_url(self, run):
        return f"https://dev.azure.com/acme/p/_build/results?buildId={run}"

    def stack_run(self, run):
        base = {"run": run, "url": self.run_url(run)}
        if self.runs[run] == "approved":
            return dict(base, **dict(PLANNED, phase="done", outputs={"job_name": "artifact-reader"}))
        return dict(base, **dict(PLANNED, sha256=f"{run:064d}", approval=f"ap-{run}"))

    def answer(self, approval, ok, comment):
        self.answers.append((approval, ok))
        self.runs[int(approval[3:])] = "approved" if ok else "rejected"

    def build(self, run):
        return {"state": "completed", "result": "succeeded", "errors": [],
                "warnings": [f"Cloud Run job artifact-reader does not exist yet: pushed {IMAGE}. In Setup, set job_image to it"]}


def job_cfg(name="artifact-reader", kind="gcp_run_job"):
    return {"name": name, "target": {"kind": kind, "gcp": {"project": "proj-1", "region": "us-central1", "name": name}}}


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.root.ready()
        # The test stack declares what the real gcp-cloud-run does.
        (stacks.stack_dir("gcp-cloud-run", self.root.dir) / "variables.tf").write_text(
            'variable "gcp_project_id" { type = string }\nvariable "gcp_region" { type = string }\n'
            'variable "workload" { type = string }\nvariable "deploy_job" {\n  type    = bool\n  default = false\n}\n'
            'variable "job_image" {\n  type    = string\n  default = ""\n}\n')
        repo = self.root.dir / "repo"
        for f in ("stack-setup.yml", "build-deploy.yml"):
            p = repo / "pipelines" / stacks.rel_dir("gcp-cloud-run")
            p.mkdir(parents=True, exist_ok=True)
            (p / f).write_text("trigger: none\n")
        self.ado = FakeAdo()
        self.jobs = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                         ado=self.ado, repo_root=repo, sleep=lambda s: None)
        self.jobs._set_status("gcp-foundation", {"deployed": True, "outputs": {}})
        self.p = provision.Provisioner(self.jobs, self.ado, sleep=lambda s: None)
        self.asked, self.lines = [], []

    def yes(self, info):
        self.asked.append(info)
        return {"answer": "yes", "plan_sha256": info["plan_sha256"]}

    def test_covers_only_kinds_with_a_stack(self):
        self.assertEqual(self.p.stack_for(job_cfg()), "gcp-cloud-run")
        self.assertIsNone(self.p.stack_for({"name": "x", "target": {"kind": "aws_lambda"}}))

    def test_yes_deploys_the_stack_then_the_first_image_then_the_workload(self):
        self.p.run(job_cfg(), self.yes, self.lines.append)
        stack_runs = [q[1] for q in self.ado.queued if q[0] == 27]
        self.assertEqual(len(stack_runs), 2)
        first, second = stack_runs
        self.assertEqual((first["inputs"]["workload"], first["inputs"]["deploy_job"]), ("artifact-reader", False))
        self.assertEqual((second["inputs"]["job_image"], second["inputs"]["deploy_job"]), (IMAGE, True))
        self.assertEqual([q[1] for q in self.ado.queued if q[0] == 26], [{"environment": "test", "workload": "artifact-reader"}])
        self.assertEqual(len(self.asked), 2)                         # one Yes per plan shown
        self.assertIn("artifact-reader", self.asked[0]["question"])
        self.assertEqual(self.ado.answers, [("ap-100", True), ("ap-102", True)])
        self.assertEqual(stacks.read_inputs("gcp-cloud-run", "test", self.root.state)["job_image"], IMAGE)

    def test_a_never_saved_stack_takes_the_clouds_shared_inputs(self):
        (self.root.state / "test" / "gcp-cloud-run.tfvars.json").unlink()
        stacks.write_inputs("gcp-foundation", "test", {"gcp_project_id": "proj-9", "ado_org": "o", "jfrog_url": "https://a.jfrog.io"},
                            self.root.dir, self.root.state)                         # shared: gcp_project_id = proj-9
        self.p.run(job_cfg(), self.yes, self.lines.append)
        inputs = self.ado.queued[0][1]["inputs"]
        self.assertEqual((inputs["gcp_project_id"], inputs["workload"]), ("proj-1", "artifact-reader"))   # the target's own wins
        self.assertEqual(inputs["deploy_job"], False)                                # declared default, then the first pass

    def test_no_changes_nothing_and_the_run_is_rejected(self):
        with self.assertRaises(provision.Declined):
            self.p.run(job_cfg(), lambda info: {"answer": "no"}, self.lines.append)
        self.assertEqual(self.ado.answers, [("ap-100", False)])
        self.assertEqual(len(self.ado.queued), 1)

    def test_needs_the_foundation(self):
        self.jobs._set_status("gcp-foundation", {"deployed": False, "outputs": {}})
        with self.assertRaises(provision.ProvisionError) as e:
            self.p.run(job_cfg(), self.yes, self.lines.append)
        self.assertIn("gcp-foundation", str(e.exception))
        self.assertEqual(self.ado.queued, [])

    def test_azure_names_map_back_to_the_stack_workload(self):
        self.assertEqual(provision.workload_name("azure_container_app_job", "caj-artifact-reader"), "artifact-reader")
        self.assertEqual(provision.workload_name("azure_functions_premium", "func-prem-demo-0a1b2c"), "func-prem-demo")
        self.assertEqual(provision.workload_name("gcp_run_job", "artifact-reader"), "artifact-reader")

    def test_first_image_is_read_from_the_build_warning(self):
        self.assertEqual(provision.first_artifact([f"Cloud Run job x does not exist yet: pushed {IMAGE}. In Setup, set"]), IMAGE)
        self.assertEqual(provision.first_artifact(["Cloud Run function f does not exist yet: uploaded f-abc.zip. In Setup"]), "f-abc.zip")
        self.assertIsNone(provision.first_artifact(["something else"]))


if __name__ == "__main__":
    unittest.main()
