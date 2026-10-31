import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from console.shell import Shell
from setup_console import stacks
from setup_console.jobs import JobError, Jobs
from setup_console.server import SetupServer, make_handler

FAKE_TF = Path(__file__).resolve().parents[2] / "console" / "tests" / "fixtures" / "fake_terraform"
TOKEN = "tok-setup-test"


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
''',
    "gke-cluster": '''variable "gcp_project_id" { type = string }
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
    "aks-cluster": '''variable "azure_subscription_id" { type = string }
variable "spot" {
  type    = bool
  default = false
}
''',
}
BACKENDS = {"gke-cluster": "gcs", "gcp-cloud-run": "gcs", "aks-cluster": "azurerm", "azure-container-apps": "azurerm"}
GCP_FOUNDATION = {"jfrog_url": "https://acme.jfrog.io", "ado_org": "my-org", "gcp_project_id": "proj-1"}
AZURE_FOUNDATION = {"ado_org": "my-org", "azure_subscription_id": "sub-1", "azure_identity": {"name": "id", "resource_group": "rg-ci"}}


class Root:
    """A copy of the catalogue's stack dirs with only the files the console reads, and a state root."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / ".state"
        for s in stacks.CATALOGUE:
            d = self.dir / s["id"]
            d.mkdir()
            (d / "variables.tf").write_text(VARS.get(s["id"], ""))
            if s["id"] in BACKENDS:
                (d / "backend.tf").write_text(f'terraform {{\n  backend "{BACKENDS[s["id"]]}" {{}}\n}}\n')

    def save(self, sid, values):
        return stacks.write_inputs(sid, "test", values, self.dir, self.state)

    def ready(self):
        self.save("gcp-foundation", GCP_FOUNDATION)
        self.save("azure-foundation", AZURE_FOUNDATION)
        self.save("gke-cluster", {"gcp_project_id": "proj-1", "jfrog_url": "https://acme.jfrog.io"})
        self.save("aks-cluster", {"azure_subscription_id": "sub-1"})

    def calls(self, sid):
        p = self.dir / sid / ".fake_calls"
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
        self.assertEqual((by["notes"]["kind"], by["notes"]["default"]), ("json", []))
        self.assertEqual(stacks.variables("azure-foundation", self.root.dir)[2]["kind"], "json")
        self.assertEqual(by["gcp_project_id"]["options"], "gcp_projects")
        g = {v["name"]: v for v in stacks.variables("gke-cluster", self.root.dir)}
        self.assertEqual((g["node_count"]["kind"], g["node_count"]["default"]), ("number", 1))
        self.assertTrue(g["api_token"]["sensitive"])

    def test_form_prefills_saved_then_shared_then_default(self):
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        f = {x["name"]: x for x in stacks.form("gke-cluster", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual((f["gcp_project_id"]["current"], f["gcp_project_id"]["source"]), ("proj-1", "shared"))
        self.assertEqual((f["name"]["current"], f["name"]["source"]), ("gke-jfrog-test", "default"))
        self.assertNotIn("api_token", f)
        self.root.save("gke-cluster", {"gcp_project_id": "proj-2", "name": "", "node_count": "3"})
        f = {x["name"]: x for x in stacks.form("gke-cluster", "test", self.root.dir, self.root.state)["fields"]}
        self.assertEqual((f["gcp_project_id"]["current"], f["gcp_project_id"]["source"]), ("proj-2", "saved"))
        self.assertEqual(f["name"]["source"], "default")    # blank optional input: the default applies
        self.assertEqual(f["node_count"]["current"], 3)

    def test_inputs_are_checked(self):
        for bad in ({"nope": "x"}, {"api_token": "x"}, {"node_count": "many"}, {"gcp_project_id": 5}):
            with self.assertRaises(stacks.StackError, msg=bad):
                self.root.save("gke-cluster", bad)
        with self.assertRaises(stacks.StackError):
            self.root.save("azure-foundation", {"azure_identity": "{not json"})

    def test_terraform_args_from_inputs(self):
        with self.assertRaises(stacks.StackError) as e:
            stacks.tf_args("gke-cluster", "test", self.root.dir, self.root.state)
        self.assertIn("gcp_project_id", str(e.exception))
        self.root.ready()
        init, plan = stacks.tf_args("gke-cluster", "test", self.root.dir, self.root.state)
        self.assertEqual(init, ("-reconfigure", "-backend-config=bucket=proj-1-tfstate", "-backend-config=prefix=gke-cluster/test"))
        self.assertEqual(plan, (f"-var-file={self.root.state / 'test' / 'gke-cluster.tfvars.json'}",))
        self.assertEqual(json.loads((self.root.state / "test" / "gke-cluster.tfvars.json").read_text())["gcp_project_id"], "proj-1")
        init, _ = stacks.tf_args("aks-cluster", "test", self.root.dir, self.root.state)
        self.assertIn("-backend-config=storage_account_name=stmyorgtfstate", init)
        self.assertIn("-backend-config=resource_group_name=rg-ci", init)
        self.assertEqual(stacks.tf_args("gcp-foundation", "test", self.root.dir, self.root.state)[0], ())

    def test_each_cloud_reads_only_its_own_foundation(self):
        self.root.save("gcp-foundation", GCP_FOUNDATION)          # GCP foundation alone: GCP stacks work
        self.root.save("gke-cluster", {"gcp_project_id": "proj-1"})
        self.root.save("aks-cluster", {"azure_subscription_id": "sub-1"})
        self.assertIn("-backend-config=bucket=proj-1-tfstate", stacks.tf_args("gke-cluster", "test", self.root.dir, self.root.state)[0])
        with self.assertRaises(stacks.StackError) as e:           # ...and Azure ones don't borrow from it
            stacks.tf_args("aks-cluster", "test", self.root.dir, self.root.state)
        self.assertIn("azure-foundation", str(e.exception))

    def test_unknown_stack_refused(self):
        with self.assertRaises(stacks.StackError):
            stacks.stack_dir("../etc", self.root.dir)

    def test_jfrog_host_from_inputs(self):
        self.root.ready()
        self.assertEqual(stacks.jfrog_host("gke-cluster", "test", self.root.state), "acme.jfrog.io")
        self.assertIsNone(stacks.jfrog_host("aks-cluster", "test", self.root.state))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.minted = []
        self.root.ready()
        self.jobs = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state,
                         token_source=lambda sh, host: self.minted.append(host) or "minted-token-value")

    def _plan(self, sid, changes, action="plan-apply"):
        (self.root.dir / sid / ".fake_plan.json").write_text(json.dumps(changes))
        return wait(self.jobs, self.jobs.start(sid, action))

    def test_deploy_needs_the_approved_plan(self):
        j = self._plan("gke-cluster", [{"address": "google_container_cluster.cluster", "type": "x", "change": {"actions": ["create"]}}])
        self.assertEqual(j["state"], "done", j)
        plan = j["result"]
        self.assertEqual(plan["summary"]["create"], 1)
        self.assertIn(f"-var-file={self.root.state / 'test' / 'gke-cluster.tfvars.json'}", self.root.calls("gke-cluster")[1])
        self.assertIn("-backend-config=bucket=proj-1-tfstate", self.root.calls("gke-cluster")[0])
        with self.assertRaises(JobError) as e:
            self.jobs.start("gke-cluster", "apply", {"plan_sha256": "0" * 64})
        self.assertEqual(e.exception.status, 403)
        done = wait(self.jobs, self.jobs.start("gke-cluster", "apply", {"plan_sha256": plan["sha256"]}))
        self.assertEqual(done["state"], "done", done)
        self.assertIn(["apply", "-input=false", "-lock-timeout=60s", "-no-color", "plan.bin"], self.root.calls("gke-cluster"))
        self.assertTrue([s for s in self.jobs.stacks() if s["id"] == "gke-cluster"][0]["status"]["deployed"])
        self.assertEqual(self.minted, ["acme.jfrog.io", "acme.jfrog.io"][:len(self.minted)])

    def test_destroy_needs_the_stack_name(self):
        j = self._plan("gke-cluster", [{"address": "google_container_cluster.cluster", "type": "x", "change": {"actions": ["delete"]}}],
                       action="plan-destroy")
        plan = j["result"]
        self.assertIn("-destroy", self.root.calls("gke-cluster")[1])
        self.assertEqual(plan["confirmation_phrase"], "gke-cluster")
        with self.assertRaises(JobError) as e:
            self.jobs.start("gke-cluster", "apply", {"plan_sha256": plan["sha256"]})
        self.assertEqual(e.exception.status, 403)
        done = wait(self.jobs, self.jobs.start("gke-cluster", "apply",
                                               {"plan_sha256": plan["sha256"], "typed_confirmation": "gke-cluster"}))
        self.assertEqual(done["state"], "done", done)
        self.assertTrue(done["result"]["destroyed"])

    def test_destroy_skips_jfrog_token_when_state_has_no_jfrog_resources(self):
        state = self.root.dir / "gcp-foundation" / "terraform.tfstate"
        state.write_text(json.dumps({"resources": [{"type": "google_storage_bucket"}]}))
        self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], action="plan-destroy")
        self.assertEqual(self.minted, [])
        state.write_text(json.dumps({"resources": [{"type": "platform_oidc_configuration"}]}))
        self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], action="plan-destroy")
        self.assertEqual(self.minted, ["acme.jfrog.io"])
        self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertEqual(len(self.minted), 2)

    def test_plan_refused_until_inputs_filled(self):
        (self.root.state / "test" / "aks-cluster.tfvars.json").unlink()
        with self.assertRaises(stacks.StackError):
            self.jobs.start("aks-cluster", "plan-apply")
        self.assertEqual([s["missing"] for s in self.jobs.stacks() if s["id"] == "aks-cluster"], [["azure_subscription_id"]])

    def test_apply_without_plan_refused(self):
        with self.assertRaises(JobError) as e:
            self.jobs.start("aks-cluster", "apply", {"plan_sha256": "x"})
        self.assertEqual(e.exception.status, 409)

    def test_token_never_in_log_or_result(self):
        j = self._plan("gke-cluster", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertNotIn("minted-token-value", json.dumps(j))

    def test_failed_plan_reported(self):
        (self.root.dir / "aks-cluster" / ".fake_fail_plan").write_text("")
        j = wait(self.jobs, self.jobs.start("aks-cluster", "plan-apply"))
        self.assertEqual(j["state"], "failed")
        self.assertIn("plan failed", j["error"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        self.console = SetupServer(token=TOKEN, root=self.root.dir, state_root=self.root.state, log=open("/dev/null", "w"),
                                   jobs=Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}, state_root=self.root.state))
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
        self.assertEqual([s["id"] for s in json.loads(body)["stacks"]], list(stacks.IDS))
        self.assertIn("Content-Security-Policy", hdrs)

    def test_page_carries_base(self):
        st, body, _ = self.req("GET", "/", token=None, headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(st, 200)
        self.assertIn(b'name="console-base" content="/setup"', body)
        self.assertIn(b'"/setup/static/app.js"', body)

    def test_inputs_round_trip_and_plan(self):
        st, body, _ = self.req("GET", "/api/stacks/gke-cluster/inputs")
        form = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(form["missing"], ["gcp_project_id"])   # prefilled from gcp-foundation, not saved yet
        self.assertEqual({f["name"]: f.get("current") for f in form["fields"]}["gcp_project_id"], "proj-1")
        self.assertEqual(self.req("PUT", "/api/stacks/gke-cluster/inputs", {"values": {"api_token": "x"}})[0], 400)
        st, body, _ = self.req("PUT", "/api/stacks/gke-cluster/inputs", {"values": {"gcp_project_id": "proj-9", "name": "x"}})
        self.assertEqual(st, 200)
        self.assertEqual({f["name"]: f.get("current") for f in json.loads(body)["fields"]}["name"], "x")
        self.assertEqual(self.req("POST", "/api/stacks/gke-cluster/jobs", {"action": "plan-apply"})[0], 202)
        self.assertEqual(self.req("POST", "/api/stacks/aks-cluster/jobs", {"action": "plan-apply"})[0], 400)
        self.assertEqual(self.req("POST", "/api/stacks/nope/jobs", {"action": "plan-apply"})[0], 400)
        self.assertEqual(self.req("GET", "/api/options/nope")[0], 404)

if __name__ == "__main__":
    unittest.main()
