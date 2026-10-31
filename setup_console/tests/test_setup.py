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
        self.assertEqual((by["notes"]["kind"], by["notes"]["default"]), ("json", []))
        self.assertEqual(stacks.variables("azure-foundation", self.root.dir)[2]["kind"], "json")
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
        with self.assertRaises(stacks.StackError):
            self.root.save("azure-foundation", {"azure_identity": "{not json"})

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

    def test_kubernetes_stacks_stand_alone(self):
        # The real stacks: a cluster keeps local state, needs no foundation and blocks none.
        for sid in ("gke-cluster", "aks-cluster"):
            self.assertEqual(stacks.get(sid)["kind"], "Kubernetes")
            self.assertIsNone(stacks.backend_kind(sid))
            self.assertIsNone(stacks.depends_on(sid))
        self.assertEqual({s["kind"] for s in stacks.CATALOGUE}, {"Serverless", "Kubernetes"})
        # Prefill never crosses from Serverless to Kubernetes, even in the same cloud.
        self.root.save("gcp-foundation", GCP_FOUNDATION)
        self.root.save("gcp-cloud-run", {"gcp_project_id": "proj-9"})
        f = {x["name"]: x for x in stacks.form("gke-cluster", "test", self.root.dir, self.root.state)["fields"]}
        self.assertNotIn("current", f.get("gcp_project_id", {}))

    def test_each_serverless_type_is_its_own_stack(self):
        # The real stacks: one per workload type, each standing only on its cloud's foundation.
        types = {s["id"]: s for s in stacks.CATALOGUE if s["kind"] == "Serverless" and "foundation" not in s["id"]}
        self.assertEqual(sorted(t["title"] for t in types.values() if t["group"] == "Google Cloud"),
                         ["Cloud Run function", "Cloud Run job", "Cloud Run service"])
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

    def test_unknown_stack_refused(self):
        with self.assertRaises(stacks.StackError):
            stacks.stack_dir("../etc", self.root.dir)

    def test_jfrog_host_from_inputs(self):
        self.root.ready()
        self.assertEqual(stacks.jfrog_host("gcp-cloud-run", "test", self.root.state), "acme.jfrog.io")
        self.assertIsNone(stacks.jfrog_host("azure-container-apps", "test", self.root.state))


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
        self.assertEqual(self.minted, ["acme.jfrog.io", "acme.jfrog.io"][:len(self.minted)])

    def test_apply_logs_to_its_own_job(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        planned = len(j["log"])
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": j["result"]["sha256"]}))
        self.assertTrue(any(line.startswith("Applied the Terraform plan: ok") for line in done["log"]), done["log"])
        self.assertEqual(len(self.jobs.get(j["id"])["log"]), planned)

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
        self.assertIn("-destroy", self.root.calls("gcp-cloud-run")[1])
        self.assertEqual(plan["confirmation_phrase"], "gcp-cloud-run")
        with self.assertRaises(JobError) as e:
            self.jobs.start("gcp-cloud-run", "apply", {"plan_sha256": plan["sha256"]})
        self.assertEqual(e.exception.status, 403)
        done = wait(self.jobs, self.jobs.start("gcp-cloud-run", "apply",
                                               {"plan_sha256": plan["sha256"], "typed_confirmation": "gcp-cloud-run"}))
        self.assertEqual(done["state"], "done", done)
        self.assertTrue(done["result"]["destroyed"])

    def test_destroy_skips_jfrog_token_when_state_has_no_jfrog_resources(self):
        state = stacks.stack_dir("gcp-foundation", self.root.dir) / "terraform.tfstate"
        state.write_text(json.dumps({"resources": [{"type": "google_storage_bucket"}]}))
        self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], action="plan-destroy")
        self.assertEqual(self.minted, [])
        state.write_text(json.dumps({"resources": [{"type": "platform_oidc_configuration"}]}))
        self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["delete"]}}], action="plan-destroy")
        self.assertEqual(self.minted, ["acme.jfrog.io"])
        self._plan("gcp-foundation", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertEqual(len(self.minted), 2)

    def test_plan_refused_until_inputs_filled(self):
        (self.root.state / "test" / "azure-container-apps.tfvars.json").unlink()
        with self.assertRaises(stacks.StackError):
            self.jobs.start("azure-container-apps", "plan-apply")
        self.assertEqual([s["missing"] for s in self.jobs.stacks() if s["id"] == "azure-container-apps"], [["azure_subscription_id"]])

    def test_apply_without_plan_refused(self):
        with self.assertRaises(JobError) as e:
            self.jobs.start("azure-container-apps", "apply", {"plan_sha256": "x"})
        self.assertEqual(e.exception.status, 409)

    def test_token_never_in_log_or_result(self):
        j = self._plan("gcp-cloud-run", [{"address": "a.b", "type": "x", "change": {"actions": ["create"]}}])
        self.assertNotIn("minted-token-value", json.dumps(j))

    def test_failed_plan_reported(self):
        (stacks.stack_dir("azure-container-apps", self.root.dir) / ".fake_fail_plan").write_text("")
        j = wait(self.jobs, self.jobs.start("azure-container-apps", "plan-apply"))
        self.assertEqual(j["state"], "failed")
        self.assertIn("plan failed", j["error"])
        self.assertTrue(j["log"][-1].startswith("▶ Failed: "), j["log"])

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
        self.assertEqual([s["id"] for s in json.loads(body)["stacks"]], list(stacks.IDS))
        self.assertIn("Content-Security-Policy", hdrs)

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
        self.assertEqual(self.req("POST", "/api/stacks/nope/jobs", {"action": "plan-apply"})[0], 400)
        self.assertEqual(self.req("GET", "/api/options/nope")[0], 404)

if __name__ == "__main__":
    unittest.main()
