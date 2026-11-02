import http.client
import json
import types
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from console.shell import Shell
from setup_console import jobs as jobs_module
from setup_console import stacks
from setup_console.jobs import JobError, Jobs
from setup_console.server import SetupServer, make_handler

REFRESH_403 = 'Error: Error when reading or editing Resource "artifactregistry repository \\\\"projects/p/locations/us-central1/repositories/platform\\\\"" with IAM Member: Error retrieving IAM policy for artifactregistry repository: googleapi: Error 403: The caller does not have permission'
FAKE_TF = Path(__file__).resolve().parents[2] / "console" / "tests" / "fixtures" / "fake_terraform"
TOKEN = "tok-setup-test"
jobs_module.probe_store = lambda kind, store: "unknown"   # tests never ask a real cloud about state stores


def fake_shell(log):
    return Shell(mode="live", log=log, which=lambda n: str(FAKE_TF) if n == "terraform" else None)


VARS = {
    "gcp-foundation": '''variable "jfrog_url" {
  description = "JFrog platform origin"
  type        = string
  validation {
    condition     = can(regex("^https://", var.jfrog_url))
    error_message = "https only."
  }
}
variable "ado_org" { type = string }
variable "gcp_project_id" { type = string }
variable "notes" {
  description = <<-EOT
    Multi-line { with braces }.
  EOT
  type    = list(string)
  default = []
}
''',
    "azure-foundation": '''variable "ado_org" { type = string }
variable "azure_subscription_id" { type = string }
variable "azure_identity" {
  type = object({
    name           = string
    resource_group = string
  })
}
variable "tags" {
  type    = map(string)
  default = {}
}
''',
    "gcp-cloud-run": '''variable "gcp_project_id" { type = string }
variable "name" {
  type    = string
  default = "gke-jfrog-test"
}
variable "node_count" {
  type    = number
  default = 1
}
variable "jfrog_url" {
  type    = string
  default = ""
}
variable "api_token" {
  type      = string
  sensitive = true
  default   = ""
}
''',
    "azure-container-apps": '''variable "azure_subscription_id" { type = string }
variable "spot" {
  type    = bool
  default = false
}
''',
}
VARS["gcp-jfrog-trust"] = 'variable "jfrog_url" { type = string }\nvariable "ado_org" { type = string }\n'
BACKENDS = {"gcp-cloud-run": "gcs", "azure-container-apps": "azurerm"}   # clusters keep local state
GCP_FOUNDATION = {"jfrog_url": "https://acme.jfrog.io", "ado_org": "my-org", "gcp_project_id": "proj-1"}
AZURE_FOUNDATION = {"ado_org": "my-org", "azure_subscription_id": "sub-1", "azure_identity": {"name": "id", "resource_group": "rg-ci"}}


class Root:
    """A copy of the catalogue's stack dirs with only the files the console reads, and a state root."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / ".state"
        for s in stacks.CATALOGUE:
            d = stacks.stack_dir(s["id"], self.dir)
            d.mkdir(parents=True)
            (d / "variables.tf").write_text(VARS.get(s["id"], ""))
            if s["id"] in BACKENDS:
                (d / "backend.tf").write_text(f'terraform {{\n  backend "{BACKENDS[s["id"]]}" {{}}\n}}\n')

    def save(self, sid, values):
        return stacks.write_inputs(sid, "test", values, self.dir, self.state)

    def ready(self):
        self.save("gcp-foundation", GCP_FOUNDATION)
        self.save("azure-foundation", AZURE_FOUNDATION)
        self.save("gcp-cloud-run", {"gcp_project_id": "proj-1", "jfrog_url": "https://acme.jfrog.io"})
        self.save("azure-container-apps", {"azure_subscription_id": "sub-1"})
        self.save("gcp-jfrog-trust", {"jfrog_url": "https://acme.jfrog.io", "ado_org": "my-org"})

    def calls(self, sid):
        p = stacks.stack_dir(sid, self.dir) / ".fake_calls"
        return [json.loads(l)["argv"] for l in p.read_text().splitlines()] if p.exists() else []


def wait(jobs, job):
    for _ in range(200):
        j = jobs.get(job["id"])
        if j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


class StackTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)

    def test_inputs_come_from_variables_tf(self):
        by = {v["name"]: v for v in stacks.variables("gcp-foundation", self.root.dir)}
        self.assertEqual(list(by), ["jfrog_url", "ado_org", "gcp_project_id", "notes"])
        self.assertTrue(by["jfrog_url"]["required"])
        self.assertEqual(by["jfrog_url"]["description"], "JFrog platform origin")
        self.assertEqual((by["notes"]["kind"], by["notes"]["default"]), ("list", []))
        ident = stacks.variables("azure-foundation", self.root.dir)[2]
        self.assertEqual((ident["kind"], ident["fields"]), ("object", ["name", "resource_group"]))
        self.assertEqual(by["gcp_project_id"]["options"], "gcp_projects")
        g = {v["name"]: v for v in stacks.variables("gcp-cloud-run", self.root.dir)}
        self.assertEqual((g["node_count"]["kind"], g["node_count"]["default"]), ("number", 1))
        self.assertTrue(g["api_token"]["sensitive"])

    def test_form_prefills_saved_then_shared_then_default(self):
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        f = {x["name"]: x for x in stacks.form("gcp-cloud-run", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual((f["gcp_project_id"]["current"], f["gcp_project_id"]["source"]), ("proj-1", "shared"))
        self.assertEqual((f["name"]["current"], f["name"]["source"]), ("gke-jfrog-test", "default"))
        self.assertNotIn("api_token", f)
        self.root.save("gcp-cloud-run", {"gcp_project_id": "proj-2", "name": "", "node_count": "3"})
        f = {x["name"]: x for x in stacks.form("gcp-cloud-run", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual((f["gcp_project_id"]["current"], f["gcp_project_id"]["source"]), ("proj-2", "saved"))
        self.assertEqual(f["name"]["source"], "default")    # blank optional input: the default applies
        self.assertEqual(f["node_count"]["current"], 3)

    def test_clouds_never_prefill_from_each_other(self):
        self.root.save("gcp-foundation", GCP_FOUNDATION)      # ado_org saved on the GCP side
        f = {x["name"]: x for x in stacks.form("azure-foundation", "test", self.root.dir, self.root.state)["fields"]}
        self.assertNotIn("current", f["ado_org"])
        self.root.save("azure-foundation", AZURE_FOUNDATION)
        f = {x["name"]: x for x in stacks.form("azure-container-apps", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual((f["azure_subscription_id"]["current"], f["azure_subscription_id"]["source"]), ("sub-1", "shared"))

    def test_inputs_are_checked(self):
        for bad in ({"nope": "x"}, {"api_token": "x"}, {"node_count": "many"}, {"gcp_project_id": 5}):
            with self.assertRaises(stacks.StackError, msg=bad):
                self.root.save("gcp-cloud-run", bad)
        for bad in ({"azure_identity": "{not json"}, {"azure_identity": {"name": "id"}},
                    {"azure_identity": {"name": "id", "resource_group": "rg", "x": "y"}}):
            with self.assertRaises(stacks.StackError, msg=bad):
                self.root.save("azure-foundation", bad)
        with self.assertRaises(stacks.StackError):
            self.root.save("gcp-foundation", {"notes": "[\"a\"]"})    # lists come as lists, never JSON text

    def test_lists_and_objects_are_plain_values(self):
        self.root.save("gcp-foundation", {"notes": [" a ", "", "b"]})
        self.root.save("azure-foundation", {"azure_identity": {"name": " id ", "resource_group": "rg"}})
        f = {x["name"]: x for x in stacks.form("gcp-foundation", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual(f["notes"]["current"], ["a", "b"])
        f = {x["name"]: x for x in stacks.form("azure-foundation", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual(f["azure_identity"]["current"], {"name": "id", "resource_group": "rg"})
        self.assertEqual(f["tags"]["kind"], "map")
        self.root.save("azure-foundation", {"tags": {" owner ": " me ", "": "dropped"}})
        f = {x["name"]: x for x in stacks.form("azure-foundation", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual(f["tags"]["current"], {"owner": "me"})
        with self.assertRaises(stacks.StackError):
            self.root.save("azure-foundation", {"tags": {"owner": 5}})

    def test_terraform_args_from_inputs(self):
        with self.assertRaises(stacks.StackError) as e:
            stacks.tf_args("gcp-cloud-run", "test", self.root.dir, self.root.state)
        self.assertIn("gcp_project_id", str(e.exception))
        self.root.ready()
        init, plan = stacks.tf_args("gcp-cloud-run", "test", self.root.dir, self.root.state)
        self.assertEqual(init, ("-reconfigure", "-backend-config=bucket=proj-1-tfstate", "-backend-config=prefix=gcp-cloud-run/test"))
        self.assertEqual(plan, (f"-var-file={self.root.state / 'test' / 'gcp-cloud-run.tfvars.json'}",))
        self.assertEqual(json.loads((self.root.state / "test" / "gcp-cloud-run.tfvars.json").read_text())["gcp_project_id"], "proj-1")
        init, _ = stacks.tf_args("azure-container-apps", "test", self.root.dir, self.root.state)
        self.assertIn("-backend-config=storage_account_name=stmyorgtfstate", init)
        self.assertIn("-backend-config=resource_group_name=rg-ci", init)
        self.assertEqual(stacks.tf_args("gcp-foundation", "test", self.root.dir, self.root.state)[0], ())

    def test_each_cloud_reads_only_its_own_foundation(self):
        self.root.save("gcp-foundation", GCP_FOUNDATION)          # GCP foundation alone: GCP stacks work
        self.root.save("gcp-cloud-run", {"gcp_project_id": "proj-1"})
        self.root.save("azure-container-apps", {"azure_subscription_id": "sub-1"})
        self.assertIn("-backend-config=bucket=proj-1-tfstate", stacks.tf_args("gcp-cloud-run", "test", self.root.dir, self.root.state)[0])
        with self.assertRaises(stacks.StackError) as e:           # ...and Azure ones don't borrow from it
            stacks.tf_args("azure-container-apps", "test", self.root.dir, self.root.state)
        self.assertIn("azure-foundation", str(e.exception))

    def test_kubernetes_stacks_use_their_cloud_foundation_for_state_and_pipeline(self):
        # The real stacks: a cluster keeps remote state in its own cloud's foundation store and has
        # its own stack-setup pipeline. Only JFrog OIDC stands on it (aks-jfrog-trust: its issuer
        # in JFrog), never another infrastructure stack.
        for sid, backend, dep in (("gke-cluster", "gcs", "gcp-foundation"), ("aks-cluster", "azurerm", "azure-foundation")):
            self.assertEqual(stacks.get(sid)["kind"], "Kubernetes")
            self.assertEqual(stacks.backend_kind(sid), backend)
            self.assertEqual(stacks.depends_on(sid), dep)
            self.assertTrue((stacks.REPO_ROOT / "pipelines" / stacks.rel_dir(sid) / "stack-setup.yml").is_file(), sid)
            self.assertTrue(all(stacks.get(o)["kind"] == "JFrog OIDC" for o in stacks.IDS if stacks.depends_on(o) == sid), sid)
        self.assertEqual(stacks.depends_on("aks-jfrog-trust"), "aks-cluster")
        self.assertEqual(stacks.backend_kind("aks-jfrog-trust"), "azurerm")
        self.assertEqual({s["kind"] for s in stacks.CATALOGUE}, {"Serverless", "Kubernetes", "JFrog OIDC"})
        # Prefill never crosses from Serverless to Kubernetes, even in the same cloud.
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        self.root.save("gcp-cloud-run", {"gcp_project_id": "proj-9"})
        f = {x["name"]: x for x in stacks.form("gke-cluster", "test", self.root.dir, self.root.state)["fields"]}
        self.assertNotIn("current", f.get("gcp_project_id", {}))

    def test_infrastructure_never_touches_jfrog(self):
        # Infrastructure (foundations, workload and cluster stacks) has no JFrog provider, resource,
        # input or setting: JFrog is OIDC configuration, made only once the infrastructure exists.
        import re as _re
        jfrog = _re.compile(r'jfrog/platform|platform_oidc|variable "jfrog_|var\.jfrog_|name\s*=\s*"JFROG_')
        for s in stacks.CATALOGUE:
            if s["kind"] == "JFrog OIDC":
                continue
            for f in stacks.stack_dir(s["id"]).glob("*.tf"):
                self.assertIsNone(jfrog.search(f.read_text()), f"{s['id']}/{f.name}")

    def test_jfrog_oidc_stacks_stand_on_their_own_cloud_foundation(self):
        for sid, dep in (("gcp-jfrog-trust", "gcp-foundation"), ("azure-jfrog-trust", "azure-foundation")):
            self.assertEqual(stacks.get(sid)["kind"], "JFrog OIDC")
            self.assertEqual(stacks.depends_on(sid), dep)
            self.assertIsNone(stacks.backend_kind(sid))   # local state, run here with the operator's token
            self.assertEqual(stacks.rel_dir(sid), f"oidc/{dep.split('-')[0]}/{sid}")
            self.assertIn('source  = "jfrog/platform"', (stacks.stack_dir(sid) / "versions.tf").read_text())

    def test_jfrog_inputs_saved_on_a_foundation_move_to_its_clouds_trust(self):
        # Inputs saved before the split: JFrog values leave the infrastructure inputs (and its
        # prefill) for the cloud's own JFrog OIDC stack; never another cloud's, never overwriting.
        import tempfile
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state)
        d = state / "test"
        d.mkdir()
        (d / "gcp-foundation.tfvars.json").write_text(json.dumps(
            {"gcp_project_id": "p1", "ado_org": "o", "jfrog_url": "https://a.jfrog.io", "jfrog_reader_group": "r",
             "console_operators": ["x@y.z"]}))
        (d / "azure-foundation.tfvars.json").write_text(json.dumps({"ado_org": "o", "jfrog_url": "https://b.jfrog.io"}))
        (d / "azure-jfrog-trust.tfvars.json").write_text(json.dumps({"jfrog_url": "https://kept.jfrog.io"}))
        (d / "shared-serverless-google-cloud.json").write_text(json.dumps({"gcp_project_id": "p1", "jfrog_url": "https://a.jfrog.io"}))
        stacks.separate_jfrog_inputs("test", state_root=state)
        self.assertEqual(stacks.read_inputs("gcp-foundation", "test", state), {"gcp_project_id": "p1", "ado_org": "o"})
        self.assertEqual(stacks.read_inputs("gcp-jfrog-trust", "test", state),
                         {"jfrog_url": "https://a.jfrog.io", "jfrog_reader_group": "r", "console_operators": ["x@y.z"]})
        self.assertEqual(stacks.read_inputs("azure-foundation", "test", state), {"ado_org": "o"})
        self.assertEqual(stacks.read_inputs("azure-jfrog-trust", "test", state), {"jfrog_url": "https://kept.jfrog.io"})
        self.assertEqual(json.loads((d / "shared-serverless-google-cloud.json").read_text()), {"gcp_project_id": "p1"})

    def test_foundation_environments_cover_every_stack_setup_pipeline(self):
        # Each foundation makes the approval environment for exactly the stacks of its cloud that
        # have a stack-setup pipeline (pipelines/<kind>/<cloud>/<stack>/stack-setup.yml).
        import re as _re
        for cloud in ("gcp", "azure"):
            tf = (stacks.STACKS_DIR / "serverless" / cloud / f"{cloud}-foundation" / "environments.tf").read_text()
            m = _re.search(r"pipeline_stacks = \[([^\]]*)\]", tf)
            listed = set(_re.findall(r'"([a-z0-9-]+)"', m.group(1)) if m else
                         _re.findall(r'"([a-z0-9-]+)"\s*=', _re.search(r"pipeline_stack_kind = \{([^}]*)\}", tf).group(1)))
            have = {p.parent.name for k in ("serverless", "kubernetes")
                    for p in (stacks.REPO_ROOT / "pipelines" / k).glob(f"{cloud}/*/stack-setup.yml")}
            self.assertEqual(listed, have, cloud)
            self.assertNotIn("oidc", tf)   # infrastructure registers nothing for JFrog OIDC
        # The JFrog OIDC stacks' pipelines and environments belong to their cloud's trust stack.
        tf = (stacks.STACKS_DIR / "oidc" / "azure" / "azure-jfrog-trust" / "pipelines.tf").read_text()
        listed = set(_re.findall(r'"([a-z0-9-]+)"\s*=\s*\{', _re.search(r"oidc_pipelines = \{(.*?)\n  \}", tf, _re.S).group(1)))
        have = {p.parent.name for p in (stacks.REPO_ROOT / "pipelines" / "oidc").glob("azure/*/stack-setup.yml")}
        self.assertEqual(listed, have)
        self.assertEqual([p for p in (stacks.REPO_ROOT / "pipelines" / "oidc").glob("gcp/*/stack-setup.yml")], [])

    def test_each_serverless_type_is_its_own_stack(self):
        # The real stacks: one per workload type, each standing only on its cloud's foundation.
        types = {s["id"]: s for s in stacks.CATALOGUE if s["kind"] == "Serverless" and "foundation" not in s["id"]}
        self.assertEqual(sorted(t["title"] for t in types.values() if t["group"] == "Google Cloud"),
                         ["Cloud Run function", "Cloud Run job", "Cloud Run service", "Cloud Run worker pool"])
        self.assertEqual(len([t for t in types.values() if t["group"] == "Azure"]), 5)
        for sid, t in types.items():
            want = {"Google Cloud": "gcp-foundation", "Azure": "azure-foundation"}[t["group"]]
            self.assertEqual(stacks.depends_on(sid), want, sid)
            self.assertTrue(any(v["name"] == "workload" for v in stacks.variables(sid)), sid)
        # A workload's own name never prefills another workload stack.
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        stacks.write_inputs("gcp-cloud-run", "test", {"gcp_project_id": "proj-9"}, self.root.dir, self.root.state)
        shared = json.loads((self.root.state / "test" / "shared-serverless-google-cloud.json").read_text())
        self.assertTrue(set(shared) <= stacks.SHARED_INPUTS, shared)

    def test_pipeline_inputs_get_their_declared_types_back(self):
        # ADO hands object parameters over with every scalar as text ("False", "1").
        raw = {"spot": "False", "azure_subscription_id": "sub-1"}
        self.assertEqual(stacks.typed_inputs("azure-container-apps", raw, self.root.dir),
                         {"spot": False, "azure_subscription_id": "sub-1"})
        self.assertEqual(stacks.typed_inputs("gcp-cloud-run", {"node_count": "2", "name": "True"}, self.root.dir),
                         {"node_count": 2, "name": "True"})            # a string stays a string
        with self.assertRaises(stacks.StackError):
            stacks.typed_inputs("azure-container-apps", {"spot": "maybe"}, self.root.dir)

    def test_unknown_stack_refused(self):
        with self.assertRaises(stacks.StackError):
            stacks.stack_dir("../etc", self.root.dir)

    def test_jfrog_host_from_inputs(self):
        self.root.ready()
        self.assertEqual(stacks.jfrog_host("gcp-cloud-run", "test", self.root.state), "acme.jfrog.io")
        self.assertIsNone(stacks.jfrog_host("azure-container-apps", "test", self.root.state))


class FakeAdo:
    """Scripted ADO: each stack_run call returns the next phase of the run."""

    def __init__(self, phases):
        self.phases, self.queued, self.answers = list(phases), [], []
        self.output = []   # step output the run has written so far (stream returns what is new)

    def stream(self, run, seen):
        new = self.output[seen.get("n", 0):]
        seen["n"] = len(self.output)
        return new

    def pipeline(self, path):
        return 27, "Cloud Run job - Infrastructure"

    def queue(self, pid, params):
        self.queued.append((pid, params))
        return 101

    def run_url(self, run):
        return f"https://dev.azure.com/acme/p/_build/results?buildId={run}"

    def stack_run(self, run):
        p = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        return dict({"run": run, "url": self.run_url(run)}, **p)

    def answer(self, approval, ok, comment):
        self.answers.append((approval, ok))


PLANNED = {"phase": "waiting_approval", "approval": "ap-1", "sha256": "a" * 64, "summary": {"create": 2},
           "changes": [{"action": "create", "address": "google_service_account.workload"}], "destructive": []}


class AdoJobTests(unittest.TestCase):
    """Workload stacks with a stack-setup pipeline deploy and destroy in Azure DevOps, never here."""

    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.root.ready()
        self.repo = self.root.dir / "repo"
        p = self.repo / "pipelines" / stacks.rel_dir("gcp-cloud-run")
        p.mkdir(parents=True)
        (p / "stack-setup.yml").write_text("trigger: none\n")

    def jobs(self, phases):
        self.ado = FakeAdo(phases)
        j = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                 ado=self.ado, repo_root=self.repo, sleep=lambda s: None)
        for f in ("gcp-foundation", "azure-foundation"):
            j._set_status(f, {"deployed": True, "outputs": {}})
        return j

    def test_deploy_queues_the_pipeline_and_approves_the_shown_plan(self):
        jobs = self.jobs([{"phase": "planning"}, PLANNED])
        j = wait(jobs, jobs.start("gcp-cloud-run", "plan-apply"))
        self.assertEqual(j["state"], "done", j)
        pid, params = self.ado.queued[0]
        self.assertEqual((pid, params["action"], params["environment"]), (27, "apply", "test"))
        self.assertEqual(params["inputs"]["gcp_project_id"], "proj-1")
        self.assertEqual(params["state"]["gcp_project_id"], "proj-1")
        self.assertEqual(self.root.calls("gcp-cloud-run"), [])          # no terraform here
        self.assertEqual(j["result"]["summary"], {"create": 2})
        self.assertIn("buildId=101", "\n".join(j["log"]))
        with self.assertRaises(JobError):
            jobs.start("gcp-cloud-run", "apply", {"plan_sha256": "b" * 64})
        self.assertEqual(self.ado.answers, [])
        self.ado.phases = [{"phase": "applying"}, dict(PLANNED, phase="done", outputs={"job_name": "artifact-reader"})]
        done = wait(jobs, jobs.start("gcp-cloud-run", "apply", {"plan_sha256": "a" * 64}))
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(self.ado.answers, [("ap-1", True)])
        row = [s for s in jobs.stacks() if s["id"] == "gcp-cloud-run"][0]
        self.assertEqual(row["status"]["outputs"], {"job_name": "artifact-reader"})

    def test_the_runs_own_output_streams_into_the_log_once(self):
        jobs = self.jobs([{"phase": "planning"}, PLANNED])
        self.ado.output = ["── terraform apply plan (OIDC)", "Plan: 2 to add, 0 to change, 0 to destroy."]
        j = wait(jobs, jobs.start("gcp-cloud-run", "plan-apply"))
        self.assertEqual(j["log"].count("Plan: 2 to add, 0 to change, 0 to destroy."), 1, j["log"])
        self.ado.output += ["── terraform apply (OIDC)", "Apply complete! Resources: 2 added, 0 changed, 0 destroyed."]
        self.ado.phases = [{"phase": "applying"}, dict(PLANNED, phase="done", outputs={})]
        done = wait(jobs, jobs.start("gcp-cloud-run", "apply", {"plan_sha256": "a" * 64}))
        self.assertIn("Apply complete! Resources: 2 added, 0 changed, 0 destroyed.", done["log"])
        self.assertNotIn("Plan: 2 to add, 0 to change, 0 to destroy.", done["log"])   # the plan's lines are not repeated

    def test_destroy_needs_the_typed_name_and_discard_rejects_the_run(self):
        jobs = self.jobs([dict(PLANNED, destructive=["google_service_account.workload"], summary={"delete": 1})])
        j = wait(jobs, jobs.start("gcp-cloud-run", "plan-destroy"))
        self.assertEqual(self.ado.queued[0][1]["action"], "destroy")
        self.assertEqual(j["result"]["confirmation_phrase"], "gcp-cloud-run")
        with self.assertRaises(JobError):
            jobs.start("gcp-cloud-run", "apply", {"plan_sha256": "a" * 64})
        jobs.discard("gcp-cloud-run")
        self.assertEqual(self.ado.answers, [("ap-1", False)])

    def test_a_failed_run_fails_the_job_with_its_reason(self):
        jobs = self.jobs([{"phase": "failed", "error": "Error: Invalid value for input variable"}])
        j = wait(jobs, jobs.start("gcp-cloud-run", "plan-apply"))
        self.assertEqual(j["state"], "failed")
        self.assertIn("Invalid value for input variable", j["error"])

    def test_no_changes_needs_no_approval(self):
        jobs = self.jobs([{"phase": "done", "summary": {}, "changes": [], "destructive": [], "sha256": "c" * 64}])
        j = wait(jobs, jobs.start("gcp-cloud-run", "plan-apply"))
        self.assertEqual(j["state"], "done")
        self.assertIsNone(j["result"]["plan"] if "plan" in j["result"] else None)
        self.assertEqual([s["plan"] for s in jobs.stacks() if s["id"] == "gcp-cloud-run"], [None])

    def _restarted(self, phases):
        """A console that died mid-run (its poll never returns) and a fresh one on the same state."""
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.ado = FakeAdo(phases)
        old = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                   ado=self.ado, repo_root=self.repo, sleep=lambda s: gate.wait())
        for f in ("gcp-foundation", "azure-foundation"):
            old._set_status(f, {"deployed": True, "outputs": {}})
        new = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                   ado=self.ado, repo_root=self.repo, sleep=lambda s: None)
        for f in ("gcp-foundation", "azure-foundation"):   # the fresh console reads them again on load
            new._set_status(f, {"deployed": True, "outputs": {}})
        return old, new

    def test_an_apply_survives_a_restart_and_is_read_from_ado(self):
        old, _ = self._restarted([PLANNED])
        wait(old, old.start("gcp-cloud-run", "plan-apply"))
        self.ado.phases = [{"phase": "applying"}]
        jid = old.start("gcp-cloud-run", "apply", {"plan_sha256": "a" * 64})["id"]
        for _ in range(200):   # the apply has its ADO run on disk before the poll blocks
            if (old.get(jid).get("log") or []) and self.ado.answers:
                break
            time.sleep(0.01)
        new = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                   ado=self.ado, repo_root=self.repo, sleep=lambda s: None)
        self.assertEqual(new.get(jid)["state"], "running")            # still applying in ADO
        self.ado.phases = [dict(PLANNED, phase="done", outputs={"job_name": "artifact-reader"})]
        j = new.get(jid)
        self.assertEqual(j["state"], "done", j)
        self.assertIn(jid, [r["id"] for r in new.history() if r["state"] == "done"])
        row = [s for s in new.stacks() if s["id"] == "gcp-cloud-run"][0]
        self.assertTrue(row["status"]["deployed"])

    def test_a_plan_waiting_on_approval_survives_a_restart(self):
        old, new = self._restarted([{"phase": "planning"}])
        jid = old.start("gcp-cloud-run", "plan-apply")["id"]
        for _ in range(200):
            if self.ado.queued and "ado_run" in json.loads((Path(self.root.state) / "test" / "jobs" / f"{jid}.json").read_text()):
                break
            time.sleep(0.01)
        self.ado.phases = [PLANNED]
        j = new.get(jid)
        self.assertEqual((j["state"], j["result"]["sha256"]), ("done", "a" * 64), j)
        self.ado.phases = [dict(PLANNED, phase="done", outputs={})]
        done = wait(new, new.start("gcp-cloud-run", "apply", {"plan_sha256": "a" * 64}))
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(self.ado.answers, [("ap-1", True)])

    def test_a_local_run_cut_by_a_restart_reads_as_stopped(self):
        jobs = self.jobs([PLANNED])
        d = Path(self.root.state) / "test" / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "0123456789ab.json").write_text(json.dumps({"id": "0123456789ab", "stack": "gcp-foundation", "action": "plan-apply",
                                                         "state": "running", "started": 1, "log": [], "log_times": []}))
        j = jobs.get("0123456789ab")
        self.assertEqual(j["state"], "failed")
        self.assertIn("restarted", j["error"])

    def test_stacks_without_a_pipeline_still_run_here(self):
        jobs = self.jobs([PLANNED])
        (stacks.stack_dir("azure-container-apps", self.root.dir) / ".fake_plan.json").write_text("[]")
        wait(jobs, jobs.start("azure-container-apps", "plan-apply"))
        self.assertEqual(self.ado.queued, [])
        self.assertTrue(self.root.calls("azure-container-apps"))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.minted = []
        self.root.ready()
        self.jobs = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                         token_source=lambda sh, host: self.minted.append(host) or "minted-token-value")
        for f in ("gcp-foundation", "azure-foundation"):   # the stacks below stand on them
            self.jobs._set_status(f, {"deployed": True, "outputs": {}})

    def _plan(self, sid, changes, action="plan-apply"):
        (stacks.stack_dir(sid, self.root.dir) / ".fake_plan.json").write_text(json.dumps(changes))
        return wait(self.jobs, self.jobs.start(sid, action))

    def test_deploy_needs_the_approved_plan(self):
        j = self._plan("gcp-cloud-run", [{"address": "google_container_cluster.cluster", "type": "x", "change": {"actions": ["create"]}}])
        self.assertEqual(j["state"], "done", j)
        plan = j["result"]
        self.assertEqual(plan["summary"]["create"], 1)
        self.assertIn(f"-var-file={self.root.state / 'test' / 'gcp-cloud-run.tfvars.json'}", self.root.calls("gcp-cloud-run")[1])
        self.assertIn("-backend-config=bucket=proj-1-tfstate", self.root.calls("gcp-cloud-run")[0])
        with self.assertRaises(JobError) as e:
            self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": "0" * 64})
        self.assertEqual(e.exception.status, 403)
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": plan["sha256"]}))
        self.assertEqual(done["state"], "done", done)
        self.assertIn(["apply", "-input=false", "-lock-timeout=60s", "-no-color", "plan.bin"], self.root.calls("gcp-cloud-run"))
        self.assertTrue([s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]["status"]["deployed"])
        self.assertEqual(self.minted, [])   # infrastructure: no JFrog token, even with a jfrog_url input

    def test_a_plan_with_no_changes_leaves_nothing_to_approve(self):
        first = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertTrue([s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]["plan"])
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["no-op"]}}])
        self.assertEqual(j["state"], "done", j)
        self.assertTrue(j["result"]["no_changes"])
        self.assertIn("▶ No changes: the stack already matches its inputs.", j["log"])
        self.assertIsNone([s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]["plan"])
        with self.assertRaises(JobError):
            self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": first["result"]["sha256"]})

    def test_apply_logs_to_its_own_job(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        planned = len(j["log"])
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": j["result"]["sha256"]}))
        self.assertTrue(any(line.startswith("Applied the Terraform plan: ok") for line in done["log"]), done["log"])
        self.assertEqual(len(self.jobs.get(j["id"])["log"]), planned)

    def test_finished_changes_are_kept_and_listed(self):
        # Runs lists every deploy and destroy after a restart; automatic state reads are not kept.
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        wait(self.jobs, self.jobs.start("gcp-cloud-run", "status"))
        again = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state)
        rows = again.history()
        self.assertEqual([(r["id"], r["stack"], r["action"], r["state"], r["cloud"]) for r in rows],
                         [(j["id"], "gcp-cloud-run", "plan-apply", "done", "gcp")])
        self.assertNotIn("log", rows[0])
        kept = again.get(j["id"])
        self.assertEqual(kept["log"], j["log"])
        self.assertNotIn("outputs", kept["result"] or {})
        self.assertEqual(again.remove(j["id"]), {"deleted": j["id"]})
        self.assertEqual(again.history(), [])
        for bad in (j["id"], "../../x"):
            with self.assertRaises(JobError) as e:
                again.remove(bad)
            self.assertEqual(e.exception.status, 404)

    def test_a_destroyed_stack_moves_its_runs_to_the_archive(self):
        # Gone infrastructure leaves Runs: its runs stay readable in the archive for 7 days, then go.
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": j["result"]["sha256"]}))
        self.assertEqual(len([r for r in self.jobs.history() if r["stack"] == "gcp-cloud-run"]), 2)
        d = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], "plan-destroy")
        self.assertEqual([r["id"] for r in self.jobs.history() if r["stack"] == "gcp-cloud-run"][0], d["id"])   # plan waits: kept
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": d["result"]["sha256"],
                                                                          "typed_confirmation": "gcp-cloud-run"}))
        self.assertEqual(done["state"], "done", done)
        self.assertEqual([r for r in self.jobs.history() if r["stack"] == "gcp-cloud-run"], [])
        arch = self.jobs.archived()
        self.assertEqual(len(arch), 4)
        self.assertTrue(all(r["archived"] and r["expires_at"] > time.time() + 6 * 86400 for r in arch))
        self.assertTrue(self.jobs.get(j["id"])["archived"])
        self.assertTrue(self.jobs.remove_archived(arch[-1]["id"]))
        self.assertEqual(len(self.jobs.archived()), 3)
        self.assertFalse(self.jobs.remove_archived("../x"))
        self.assertEqual(self.jobs.purge_archive(now=time.time() + 7 * 86400 + 1).__len__(), 3)
        with self.assertRaises(JobError):
            self.jobs.get(j["id"])

    def test_kept_runs_are_redacted(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        secret = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2lnbmF0dXJl"
        self.jobs._jobs[j["id"]]["log"].append(f"token {secret}")
        self.jobs._keep(self.jobs._jobs[j["id"]])
        text = (self.root.state / "test" / "jobs" / f"{j['id']}.json").read_text()
        self.assertNotIn(secret, text)

    def test_stacks_name_the_running_job(self):
        # A page that closed the drawer (or was reloaded) finds the running job here and follows it again.
        gate = threading.Event()

        def held_shell(log):
            sh = fake_shell(log)
            run = sh.run
            sh.run = lambda *a, **kw: gate.wait(5) and run(*a, **kw)
            return sh
        self.jobs.shell_factory = held_shell
        job = self.jobs.start("gcp-cloud-run", "status")
        row = lambda: [s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]
        try:
            self.assertEqual(row()["job"], {"id": job["id"], "action": "status"})
        finally:
            gate.set()
        wait(self.jobs, job)
        self.assertIsNone(row()["job"])

    def test_destroy_needs_the_stack_name(self):
        j = self._plan("gcp-cloud-run", [{"address": "google_container_cluster.cluster", "type": "x", "change": {"actions": ["delete"]}}],
                       action="plan-destroy")
        plan = j["result"]
        planned = [c for c in self.root.calls("gcp-cloud-run") if c[0] == "plan" and "-refresh-only" not in c]
        self.assertIn("-destroy", planned[0])   # after the read-only check of which resources still exist
        self.assertEqual(plan["confirmation_phrase"], "gcp-cloud-run")
        with self.assertRaises(JobError) as e:
            self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": plan["sha256"]})
        self.assertEqual(e.exception.status, 403)
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply",
                                               {"plan_sha256": plan["sha256"], "typed_confirmation": "gcp-cloud-run"}))
        self.assertEqual(done["state"], "done", done)
        self.assertTrue(done["result"]["destroyed"])

    def test_destroy_skips_jfrog_token_when_state_has_no_jfrog_resources(self):
        state = stacks.stack_dir("gcp-jfrog-trust", self.root.dir) / "terraform.tfstate"
        state.write_text(json.dumps({"resources": [{"type": "google_service_account"}]}))
        self._plan("gcp-jfrog-trust", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], action="plan-destroy")
        self.assertEqual(self.minted, [])
        state.write_text(json.dumps({"resources": [{"type": "platform_oidc_configuration"}]}))
        self._plan("gcp-jfrog-trust", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], action="plan-destroy")
        self.assertEqual(self.minted, ["acme.jfrog.io"])
        self._plan("gcp-jfrog-trust", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertEqual(len(self.minted), 2)

    def test_infrastructure_terraform_never_sees_a_jfrog_token(self):
        self.jobs.environ = {"JFROG_ACCESS_TOKEN": "operator-admin-token", "JFROG_URL": "https://acme.jfrog.io"}
        for sid in ("gcp-foundation", "gcp-cloud-run"):
            self._plan(sid, [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
            p = stacks.stack_dir(sid, self.root.dir) / ".fake_calls"
            self.assertFalse(any(json.loads(l)["jfrog_token_env"] for l in p.read_text().splitlines()), sid)
        self._plan("gcp-jfrog-trust", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        p = stacks.stack_dir("gcp-jfrog-trust", self.root.dir) / ".fake_calls"
        self.assertTrue(any(json.loads(l)["jfrog_token_env"] for l in p.read_text().splitlines()))

    def test_plan_refused_until_inputs_filled(self):
        (self.root.state / "test" / "azure-container-apps.tfvars.json").unlink()
        with self.assertRaises(stacks.StackError):
            self.jobs.start("azure-container-apps", "plan-apply")
        self.assertEqual([s["missing"] for s in self.jobs.stacks() if s["id"] == "azure-container-apps"], [["azure_subscription_id"]])

    def test_apply_without_plan_refused(self):
        with self.assertRaises(JobError) as e:
            self.jobs.start("azure-container-apps", "apply", {"plan_sha256": "x"})
        self.assertEqual(e.exception.status, 409)

    def test_changes_are_audited_with_the_client(self):
        self.jobs.audit("gcp-cloud-run", "plan-destroy", "Mozilla/5.0 test")
        self.jobs.audit("gcp-cloud-run", "status", "Mozilla/5.0 test")       # reads are not audited
        rows = [json.loads(l) for l in (self.root.state / "test" / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([(r["stack"], r["action"], r["client"]) for r in rows], [("gcp-cloud-run", "plan-destroy", "Mozilla/5.0 test")])

    def test_token_never_in_log_or_result(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertNotIn("minted-token-value", json.dumps(j))

    def test_every_log_line_has_a_time(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertTrue(j["log"])
        self.assertEqual(len(j["log_times"]), len(j["log"]))
        self.assertTrue(all(isinstance(t, float) for t in j["log_times"]))
        self.assertEqual(j["log_times"], sorted(j["log_times"]))

    def test_failed_plan_reported(self):
        (stacks.stack_dir("azure-container-apps", self.root.dir) / ".fake_fail_plan").write_text("")
        j = wait(self.jobs, self.jobs.start("azure-container-apps", "plan-apply"))
        self.assertEqual(j["state"], "failed")
        self.assertIn("plan failed", j["error"])
        self.assertTrue(j["log"][-1].startswith("▶ Failed: "), j["log"])

    def test_failed_run_says_what_to_do_next(self):
        # Any failure: the cause in words, then Try again and Check status.
        (stacks.stack_dir("azure-container-apps", self.root.dir) / ".fake_fail_plan").write_text("")
        j = wait(self.jobs, self.jobs.start("azure-container-apps", "plan-apply"))
        self.assertEqual([a["action"] for a in j["next"]["actions"]], ["plan-apply", "status"])
        self.assertTrue(j["next"]["cause"])
        failed = {s["id"]: s for s in self.jobs.stacks()}["azure-container-apps"]["failed"]
        self.assertEqual(failed["next"], j["next"])

    def test_unreadable_resources_offer_forget_for_local_state(self):
        # The foundation's resources were deleted outside the console: the destroy plan cannot read
        # them (403). Next: Forget its local state (no cloud call), or Check status, or try again.
        sid = "gcp-foundation"
        for x in ("gcp-cloud-run", "gcp-jfrog-trust"):   # read: nothing stands on it
            self.jobs._set_status(x, {"deployed": False, "outputs": {}})
        d = stacks.stack_dir(sid, self.root.dir)
        (d / "terraform.tfstate").write_text('{"version": 4, "resources": [{"type": "x"}]}')
        (d / ".fake_fail_plan").write_text(REFRESH_403)
        j = wait(self.jobs, self.jobs.start(sid, "plan-destroy"))
        self.assertEqual(j["state"], "failed")
        self.assertIn("deleted outside the console", j["next"]["cause"])
        self.assertEqual([a["action"] for a in j["next"]["actions"]], ["forget", "status", "plan-destroy"])
        # A workload (remote state) is not offered Forget.
        (stacks.stack_dir("gcp-cloud-run", self.root.dir) / ".fake_fail_plan").write_text(REFRESH_403)
        w = wait(self.jobs, self.jobs.start("gcp-cloud-run", "plan-destroy"))
        self.assertNotIn("forget", [a["action"] for a in w["next"]["actions"]])

    def test_forget_moves_local_state_aside_after_typed_name(self):
        sid = "gcp-foundation"
        d = stacks.stack_dir(sid, self.root.dir)
        (d / "terraform.tfstate").write_text('{"version": 4, "resources": [{"type": "x"}]}')
        (d / "terraform.tfstate.backup").write_text("{}")
        with self.assertRaises(JobError) as e:
            self.jobs.start(sid, "forget", {"typed_confirmation": "nope"})
        self.assertEqual(e.exception.status, 403)
        j = wait(self.jobs, self.jobs.start(sid, "forget", {"typed_confirmation": sid}))
        self.assertEqual(j["state"], "done", j)
        self.assertFalse((d / "terraform.tfstate").exists())
        self.assertFalse((d / "terraform.tfstate.backup").exists())
        kept = list((self.root.state / "test" / "forgotten").glob(f"{sid}-*.tfstate"))
        self.assertEqual(len(kept), 1)
        self.assertIn('"resources"', kept[0].read_text())
        self.assertFalse(self.jobs.deployed(sid))
        self.assertEqual(self.root.calls(sid), [])   # Terraform never ran: nothing in the cloud was touched
        # Only stacks with local state can be forgotten.
        with self.assertRaises(JobError):
            self.jobs.start("gcp-cloud-run", "forget", {"typed_confirmation": "gcp-cloud-run"})

    def test_log_tells_each_stage_in_words(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        said = [line[2:] for line in j["log"] if line.startswith("▶ ")]
        self.assertTrue(said[0].startswith("Planning a deploy of Serverless · Google Cloud · Cloud Run job (gcp-cloud-run)"), said)
        self.assertIn("Terraform state: Cloud Storage bucket proj-1-tfstate, prefix gcp-cloud-run/test.", said)
        self.assertIn("Plan ready: 1 to add. Nothing changes until you approve it.", said)
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": j["result"]["sha256"]}))
        said = [line[2:] for line in done["log"] if line.startswith("▶ ")]
        self.assertTrue(said[0].startswith("Running the approved deploy plan"), said)
        self.assertTrue(said[-1].startswith("Deployed."), said)
        self.assertNotIn(TOKEN, "\n".join(done["log"]))

    def test_refresh_all_reads_every_ready_stack_once(self):
        ids = self.jobs.refresh_all()
        done = [wait(self.jobs, {"id": i}) for i in ids]
        checked = {d["stack"] for d in done}
        self.assertIn("gcp-cloud-run", checked)
        self.assertTrue(all(d["action"] == "status" for d in done))
        self.assertEqual(self.jobs.refresh_all(), [])   # known now: nothing started again

    def test_refresh_all_leaves_stacks_alone_until_their_foundation_is_deployed(self):
        self.jobs._status.clear()
        (stacks.stack_dir("gcp-foundation", self.root.dir) / ".fake_fail_init").write_text("")   # not deployed: read fails
        ids = self.jobs.refresh_all()
        done = [wait(self.jobs, {"id": i}) for i in ids]
        self.assertNotIn("gcp-cloud-run", {d["stack"] for d in done})
        rows = {s["id"]: s for s in self.jobs.stacks()}
        self.assertIsNone(rows["gcp-cloud-run"]["status"])
        self.assertIsNone(rows["gcp-foundation"]["failed"])   # an automatic read never shows as a failed run

    def test_deploy_refused_while_its_foundation_is_not_deployed(self):
        self.jobs._set_status("gcp-foundation", {"deployed": False, "outputs": {}})
        row = [s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]
        self.assertEqual(row["depends_on"], "gcp-foundation")
        self.assertEqual(row["needs"], ["gcp-foundation"])
        with self.assertRaises(JobError) as e:
            self.jobs.start("gcp-cloud-run", "plan-apply")
        self.assertEqual(e.exception.status, 409)
        self.assertIn("gcp-foundation", str(e.exception))
        self.jobs._set_status("gcp-foundation", {"deployed": True, "outputs": {}})
        self.assertEqual(wait(self.jobs, self.jobs.start("gcp-cloud-run", "status"))["state"], "done")

    def test_a_foundation_not_read_yet_does_not_satisfy_the_order(self):
        self.jobs._status.pop("gcp-foundation")
        self.assertEqual([s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]["needs"], ["gcp-foundation"])
        with self.assertRaises(JobError):
            self.jobs.start("gcp-cloud-run", "plan-apply")

    def test_deploying_a_foundation_reads_its_stacks_again(self):
        self.jobs._set_status("gcp-cloud-run", {"deployed": False, "outputs": {}, "error": "bucket doesn't exist"})
        j = self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        wait(self.jobs, self.jobs.start("gcp-foundation", "apply", {"plan_sha256": j["result"]["sha256"]}))
        reread = [x for x in self.jobs._jobs.values() if x["stack"] == "gcp-cloud-run" and x["action"] == "status"]
        self.assertTrue(reread)
        wait(self.jobs, reread[0])
        self.assertNotIn("error", [s for s in self.jobs.stacks() if s["id"] == "gcp-cloud-run"][0]["status"])
        self.assertFalse([x for x in self.jobs._jobs.values() if x["stack"] == "azure-container-apps"])

    def test_foundation_destroy_refused_while_stacks_stand_on_it(self):
        self.jobs._set_status("gcp-cloud-run", {"deployed": True, "outputs": {}})
        row = [s for s in self.jobs.stacks() if s["id"] == "gcp-foundation"][0]
        self.assertEqual(row["dependents"], ["gcp-cloud-run"])
        with self.assertRaises(JobError) as e:
            self.jobs.start("gcp-foundation", "plan-destroy")
        self.assertEqual(e.exception.status, 409)
        self.assertIn("gcp-cloud-run", str(e.exception))
        # Other clouds are never involved.
        self.assertEqual([s for s in self.jobs.stacks() if s["id"] == "azure-foundation"][0]["dependents"], [])

    def test_upstreams_are_transitive_and_cover_every_stack_used(self):
        self.assertEqual(stacks.upstreams("aks-jfrog-trust"), ("aks-cluster", "azure-jfrog-trust", "azure-foundation"))
        self.assertEqual(stacks.ancestors("aks-jfrog-trust"), ("azure-foundation", "azure-jfrog-trust", "aks-cluster"))
        self.assertEqual(stacks.descendants("azure-foundation")[0], "aks-jfrog-trust")   # destroy order
        self.assertIn("azure-jfrog-trust", stacks.descendants("azure-foundation"))
        self.assertEqual(stacks.ancestors("gcp-foundation"), ())
        self.assertEqual(stacks.build_upstreams("gcp-cloud-run"), ("gcp-foundation", "gcp-cloud-run", "gcp-jfrog-trust"))
        for s in stacks.CATALOGUE:   # never across clouds; deploy order is catalogue order
            for a in stacks.ancestors(s["id"]):
                self.assertEqual(stacks.get(a)["group"], s["group"], s["id"])
                self.assertLess(stacks.IDS.index(a), stacks.IDS.index(s["id"]), s["id"])

    def test_deploy_refused_while_any_upstream_is_missing_even_indirectly(self):
        self.root.save("aks-jfrog-trust", {})
        self.jobs._set_status("aks-cluster", {"deployed": True, "outputs": {}})
        row = [s for s in self.jobs.stacks() if s["id"] == "aks-jfrog-trust"][0]
        self.assertEqual(row["needs"], ["azure-jfrog-trust"])
        with self.assertRaisesRegex(JobError, "azure-jfrog-trust"):
            self.jobs.start("aks-jfrog-trust", "plan-apply")
        self.jobs._set_status("azure-foundation", {"deployed": False, "outputs": {}})
        row = [s for s in self.jobs.stacks() if s["id"] == "aks-jfrog-trust"][0]
        self.assertEqual(row["needs"], ["azure-foundation", "azure-jfrog-trust"])

    def test_approval_rechecks_the_upstream(self):
        # Planned while the foundation stood; it was destroyed before Approve: the apply is refused.
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.jobs._set_status("gcp-foundation", {"deployed": False, "outputs": {}})
        with self.assertRaisesRegex(JobError, "gcp-foundation") as e:
            self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": j["result"]["sha256"]})
        self.assertEqual(e.exception.status, 409)
        self.assertFalse([c for c in self.root.calls("gcp-cloud-run") if "apply" in c])

    def test_approval_rechecks_what_stands_on_it(self):
        for x in ("gcp-cloud-run", "gcp-jfrog-trust"):
            self.jobs._set_status(x, {"deployed": False, "outputs": {}})
        j = self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], "plan-destroy")
        self.jobs._set_status("gcp-cloud-run", {"deployed": True, "outputs": {}})   # deployed meanwhile
        with self.assertRaisesRegex(JobError, "gcp-cloud-run"):
            self.jobs.start("gcp-foundation", "apply", {"plan_sha256": j["result"]["sha256"], "typed_confirmation": "gcp-foundation"})

    def test_destroy_waits_for_stacks_on_it_to_be_read(self):
        self.jobs._set_status("gcp-jfrog-trust", {"deployed": False, "outputs": {}})
        row = [s for s in self.jobs.stacks() if s["id"] == "gcp-foundation"][0]
        self.assertEqual(row["unchecked"], ["gcp-cloud-run"])   # inputs saved, state not read yet
        with self.assertRaisesRegex(JobError, "check what may stand on gcp-foundation first"):
            self.jobs.start("gcp-foundation", "plan-destroy")

    def test_failed_job_named_on_its_stack(self):
        (stacks.stack_dir("azure-container-apps", self.root.dir) / ".fake_fail_plan").write_text("")
        j = wait(self.jobs, self.jobs.start("azure-container-apps", "plan-apply"))
        row = [s for s in self.jobs.stacks() if s["id"] == "azure-container-apps"][0]
        self.assertEqual(row["failed"]["id"], j["id"])
        self.assertIn("plan failed", row["failed"]["error"])

    def test_failed_status_means_not_deployed(self):
        (stacks.stack_dir("azure-container-apps", self.root.dir) / ".fake_fail_init").write_text("")
        j = wait(self.jobs, self.jobs.start("azure-container-apps", "status"))
        self.assertEqual(j["state"], "failed")
        st = [s for s in self.jobs.stacks() if s["id"] == "azure-container-apps"][0]["status"]
        self.assertFalse(st["deployed"])
        self.assertTrue(st["error"])


class DeletedOutsideTests(unittest.TestCase):
    """Resources deleted outside the console (by hand, another tool): the deletion is validated with the
    cloud and passes, instead of a read or a destroy failing on the not-found."""

    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.root.ready()
        self.repo = self.root.dir / "repo"
        p = self.repo / "pipelines" / stacks.rel_dir("gcp-cloud-run")
        p.mkdir(parents=True)
        (p / "stack-setup.yml").write_text("trigger: none\n")
        self.asked = []

    def jobs(self, answer):
        self.ado = FakeAdo([PLANNED])
        j = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state, ado=self.ado,
                 repo_root=self.repo, sleep=lambda s: None, probe=lambda kind, store: self.asked.append(store["label"]) or answer)
        for f in ("gcp-foundation", "azure-foundation"):
            j._set_status(f, {"deployed": True, "outputs": {}})
        return j

    def nothing_on(self, jobs, *sids):   # read: nothing stands on the foundation
        for x in sids:
            jobs._set_status(x, {"deployed": False, "outputs": {}})

    def status(self, jobs, sid):
        return [s for s in jobs.stacks() if s["id"] == sid][0]["status"]

    def ws(self, sid, name, text):
        (stacks.stack_dir(sid, self.root.dir) / name).write_text(text)

    def test_a_workload_whose_state_store_is_gone_reads_as_deleted(self):
        jobs = self.jobs("missing")
        j = wait(jobs, jobs.start("gcp-cloud-run", "status"))
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(self.asked, ["Cloud Storage bucket proj-1-tfstate"])
        st = self.status(jobs, "gcp-cloud-run")
        self.assertFalse(st["deployed"])
        self.assertIn("proj-1-tfstate, no longer exists", st["deleted"])
        self.assertNotIn("error", st)
        self.assertEqual(self.root.calls("gcp-cloud-run"), [])   # nothing to read: no terraform

    def test_destroying_it_passes_without_a_pipeline_run(self):
        jobs = self.jobs("missing")
        j = wait(jobs, jobs.start("gcp-cloud-run", "plan-destroy"))
        self.assertEqual(j["state"], "done", j)
        self.assertTrue(j["result"]["already_deleted"])
        self.assertEqual(self.ado.queued, [])
        self.assertIsNone([s for s in jobs.stacks() if s["id"] == "gcp-cloud-run"][0]["plan"])

    def test_an_unconfirmed_store_is_never_taken_as_deleted(self):
        jobs = self.jobs("unknown")   # no sign-in, no access: not proof of anything
        wait(jobs, jobs.start("gcp-cloud-run", "plan-destroy"))
        self.assertEqual(self.ado.queued[0][1]["action"], "destroy")

    def test_a_foundation_with_its_store_gone_is_partly_deleted(self):
        jobs = self.jobs("missing")
        self.ws("gcp-foundation", ".fake_outputs.json", json.dumps({"tfstate_bucket": {"value": "proj-1-tfstate"}}))
        j = wait(jobs, jobs.start("gcp-foundation", "status"))
        self.assertEqual(j["state"], "done", j)
        st = self.status(jobs, "gcp-foundation")
        self.assertFalse(st["deployed"])
        self.assertTrue(st["deleted"].startswith("Partly deleted outside the console"))
        self.assertEqual(st["outputs"], {"tfstate_bucket": "proj-1-tfstate"})

    def test_a_partly_deleted_foundation_reads_its_stacks_so_destroy_unlocks(self):
        """Its stacks were never read (they wait for a deployed foundation), so Destroy stayed greyed out
        behind "check what may stand on it first" for good. Reading it as partly deleted reads them."""
        jobs = self.jobs("missing")
        jobs._status.pop("gcp-foundation")
        self.ws("gcp-foundation", ".fake_outputs.json", json.dumps({"tfstate_bucket": {"value": "proj-1-tfstate"}}))
        wait(jobs, jobs.start("gcp-foundation", "status"))
        for x in [x for x in jobs._jobs.values() if x["stack"] != "gcp-foundation"]:
            wait(jobs, x)
        row = [s for s in jobs.stacks() if s["id"] == "gcp-foundation"][0]
        self.assertEqual(row["unchecked"], [])
        self.assertIn("deleted", self.status(jobs, "gcp-cloud-run"))

    def test_destroy_forgets_what_the_cloud_says_is_gone_and_plans_the_rest(self):
        jobs = self.jobs("unknown")
        repo_id = "projects/proj-1/locations/us-central1/repositories/platform"
        events = [
            {"type": "resource_drift", "change": {"resource": {"addr": "google_artifact_registry_repository.images"}, "action": "delete"}},
            {"type": "resource_drift", "change": {"resource": {"addr": "google_service_account.ado"}, "action": "delete"}},
            # Held by the deleted repository: its read fails with 403, which names the repository.
            {"type": "diagnostic", "diagnostic": {"severity": "error", "address": "google_artifact_registry_repository_iam_member.push",
                                                  "summary": f'Error retrieving IAM policy for "{repo_id}": Error 403'}},
            {"type": "diagnostic", "diagnostic": {"severity": "error", "address": "google_storage_bucket.tfstate",
                                                  "summary": "googleapi: Error 404: The specified bucket does not exist., notFound"}},
            # Lacking a token is not proof the resource is gone: it stays.
            {"type": "diagnostic", "diagnostic": {"severity": "error", "address": "platform_oidc_configuration.ado",
                                                  "summary": "Unable to Refresh Resource", "detail": '{"code":"UNAUTHORIZED"}'}},
        ]
        self.ws("gcp-foundation", ".fake_refresh.jsonl", "\n".join(json.dumps(e) for e in events))
        self.ws("gcp-foundation", ".fake_state.json", json.dumps({"values": {"root_module": {"resources": [
            {"address": "google_artifact_registry_repository.images", "values": {"id": repo_id}}]}}}))
        self.ws("gcp-foundation", ".fake_plan.json", json.dumps(
            [{"address": "platform_oidc_configuration.ado", "type": "platform_oidc_configuration", "change": {"actions": ["delete"]}}]))
        self.nothing_on(jobs, "gcp-cloud-run", "gcp-jfrog-trust")
        j = wait(jobs, jobs.start("gcp-foundation", "plan-destroy"))
        self.assertEqual(j["state"], "done", j)
        forgotten = (stacks.stack_dir("gcp-foundation", self.root.dir) / ".fake_forgotten").read_text().split()
        self.assertEqual(forgotten, ["google_artifact_registry_repository.images", "google_service_account.ado",
                                     "google_artifact_registry_repository_iam_member.push", "google_storage_bucket.tfstate"])
        self.assertEqual(j["result"]["summary"]["delete"], 1)   # the JFrog integration is still there to destroy
        self.assertIn("4 of its resources were already deleted outside the console", "\n".join(j["log"]))

    def test_destroy_with_nothing_left_passes(self):
        jobs = self.jobs("unknown")
        self.ws("gcp-foundation", ".fake_refresh.jsonl", json.dumps(
            {"type": "resource_drift", "change": {"resource": {"addr": "google_service_account.ado"}, "action": "delete"}}))
        self.ws("gcp-foundation", ".fake_plan.json", "[]")
        self.nothing_on(jobs, "gcp-cloud-run", "gcp-jfrog-trust")
        j = wait(jobs, jobs.start("gcp-foundation", "plan-destroy"))
        self.assertEqual(j["state"], "done", j)
        self.assertTrue(j["result"]["already_deleted"])
        st = self.status(jobs, "gcp-foundation")
        self.assertEqual((st["deployed"], st["deleted"]), (False, "Deleted outside the console: nothing of it is left."))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        self.console = SetupServer(token=TOKEN, root=self.root.dir, state_root=self.root.state, log=open("/dev/null", "w"),
                                   jobs=Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state))
        self.console.jobs._set_status("gcp-foundation", {"deployed": True, "outputs": {}})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.console))
        self.console.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None, token=TOKEN, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.console.port)
        h = {"Host": f"127.0.0.1:{self.console.port}", **(headers or {})}
        if token:
            h["X-Console-Token"] = token
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, r.read(), dict(r.getheaders())

    def test_token_required(self):
        self.assertEqual(self.req("GET", "/api/stacks", token=None)[0], 401)

    def test_bad_host_and_origin_refused(self):
        self.assertEqual(self.req("GET", "/api/stacks", headers={"Host": "evil:1"})[0], 403)
        self.assertEqual(self.req("GET", "/api/stacks", headers={"Origin": "http://evil"})[0], 403)

    def test_lists_catalogue(self):
        st, body, hdrs = self.req("GET", "/api/stacks")
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertEqual([s["id"] for s in data["stacks"]], [s for s in stacks.IDS if stacks.get(s)["kind"] != "JFrog OIDC"])
        self.assertEqual(data["scope"], "infra")
        self.assertIn("Content-Security-Policy", hdrs)
        self.assertEqual(self.req("GET", "/api/stacks/gcp-jfrog-trust/inputs")[0], 404)   # JFrog OIDC: not served here
        self.assertEqual(self.req("POST", "/api/stacks/aks-jfrog-trust/jobs", {"action": "plan-apply"})[0], 404)

    def test_oidc_scope_serves_only_jfrog_oidc_stacks(self):
        o = SetupServer(token=TOKEN, root=self.root.dir, state_root=self.root.state, log=open("/dev/null", "w"),
                        jobs=self.console.jobs, base="/oidc", scope="oidc")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(o))
        o.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port, self.console.port = self.console.port, o.port
        try:
            data = json.loads(self.req("GET", "/api/stacks")[1])
            self.assertEqual([s["id"] for s in data["stacks"]], ["gcp-jfrog-trust", "azure-jfrog-trust", "aks-jfrog-trust"])
            self.assertEqual(data["titles"]["aks-cluster"], "AKS cluster")   # names what it stands on, nothing more
            self.assertEqual(self.req("GET", "/api/stacks/gcp-cloud-run/inputs")[0], 404)
            self.assertEqual(self.req("POST", "/api/stacks/gcp-foundation/jobs", {"action": "plan-apply"})[0], 404)
            self.assertEqual(self.req("GET", "/api/stacks/gcp-jfrog-trust/inputs")[0], 200)
        finally:
            self.console.port = port
        self.assertEqual(self.req("PUT", "/api/stacks/gcp-cloud-run/inputs", {"values": {"gcp_project_id": "proj-9"}})[0], 200)
        self.assertEqual(self.req("POST", "/api/stacks/gcp-cloud-run/jobs", {"action": "plan-apply"})[0], 202)
        self.assertEqual(o.history(), [])
        jid = self.console.history()[0]["id"]
        with self.assertRaises(Exception):
            o.job(jid)
        with self.assertRaises(ValueError):
            SetupServer(token=TOKEN, scope="both")

    def test_page_carries_base(self):
        st, body, _ = self.req("GET", "/", token=None, headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(st, 200)
        self.assertIn(b'name="console-base" content="/setup"', body)
        self.assertIn(b'"/setup/static/app.js"', body)

    def test_inputs_round_trip_and_plan(self):
        st, body, _ = self.req("GET", "/api/stacks/gcp-cloud-run/inputs")
        form = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(form["missing"], ["gcp_project_id"])   # prefilled from gcp-foundation, not saved yet
        self.assertEqual({f["name"]: f.get("current") for f in form["fields"]}["gcp_project_id"], "proj-1")
        self.assertEqual(self.req("PUT", "/api/stacks/gcp-cloud-run/inputs", {"values": {"api_token": "x"}})[0], 400)
        st, body, _ = self.req("PUT", "/api/stacks/gcp-cloud-run/inputs", {"values": {"gcp_project_id": "proj-9", "name": "x"}})
        self.assertEqual(st, 200)
        self.assertEqual({f["name"]: f.get("current") for f in json.loads(body)["fields"]}["name"], "x")
        self.assertEqual(self.req("POST", "/api/stacks/gcp-cloud-run/jobs", {"action": "plan-apply"})[0], 202)
        self.assertEqual(self.req("POST", "/api/stacks/azure-container-apps/jobs", {"action": "plan-apply"})[0], 400)
        self.assertEqual(self.req("POST", "/api/stacks/nope/jobs", {"action": "plan-apply"})[0], 404)
        self.assertEqual(self.req("GET", "/api/options/nope")[0], 404)
        st, body, _ = self.req("GET", "/api/jobs")
        self.assertEqual(st, 200)
        self.assertEqual([r["stack"] for r in json.loads(body)["jobs"]], ["gcp-cloud-run"])
        self.assertEqual(self.req("DELETE", "/api/jobs/000000000000")[0], 404)

class LiveOptionTests(unittest.TestCase):
    """Approvers and operators are picked from live ADO, Entra and Google Cloud data, by email."""

    class Res:
        def __init__(self, data):
            self.ok, self.data = True, data

        def json(self, default=None):
            return self.data

    def console(self, data, ado=None):
        sh = types.SimpleNamespace(run=lambda argv, timeout=60, **kw: (self.argv.append(argv), LiveOptionTests.Res(data))[1])
        jobs = types.SimpleNamespace(shell_factory=lambda log: sh, ado=ado)
        self.argv = []
        return SetupServer(token=TOKEN, jobs=jobs, log=open("/dev/null", "w"))

    def test_gcp_members_are_the_projects_user_emails(self):
        c = self.console({"bindings": [{"role": "roles/owner", "members": ["user:b@x.com", "serviceAccount:sa@p.iam"]},
                                       {"role": "roles/viewer", "members": ["user:a@x.com", "user:b@x.com", "group:g@x.com"]}]})
        self.assertEqual([o["id"] for o in c.options("gcp_members", "proj-1")], ["a@x.com", "b@x.com"])
        self.assertIn("proj-1", self.argv[0])
        with self.assertRaises(Exception):
            c.options("gcp_members", "bad project; rm")

    def test_azure_users_by_email(self):
        c = self.console([{"userPrincipalName": "m@corp.com", "mail": "m@corp.com", "displayName": "M"},
                          {"userPrincipalName": "g.h_gmail.com#EXT#@t.onmicrosoft.com", "mail": None, "displayName": "G"}])
        self.assertEqual({o["id"]: o["name"] for o in c.options("azure_users")}, {"g.h@gmail.com": "G", "m@corp.com": "M"})

    def test_ado_users_from_the_organisation(self):
        ado = types.SimpleNamespace(users=lambda: [{"id": "a@x.com", "name": "A"}])
        self.assertEqual(self.console(None, ado).options("ado_users"), [{"id": "a@x.com", "name": "A"}])
        self.assertEqual(self.console(None, None).options("ado_users"), [])

    def test_fields_name_their_sources(self):
        self.assertEqual(stacks.OPTION_SOURCES["ado_approvers"], "ado_users")
        self.assertEqual(stacks.OPTION_SOURCES["console_operators"], "gcp_members")
        self.assertEqual(stacks.OPTION_SOURCES["azure_console_operators"], "azure_users")


if __name__ == "__main__":
    unittest.main()
