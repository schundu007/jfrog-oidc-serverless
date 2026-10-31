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


class Root:
    """A copy of the catalogue's stack dirs with only the files the console reads."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        for s in stacks.CATALOGUE:
            (self.dir / s["id"]).mkdir()
        (self.dir / "gke-cluster" / "env").mkdir()
        (self.dir / "gke-cluster" / "env" / "test.backend.hcl").write_text('bucket = "b"\n')
        (self.dir / "gke-cluster" / "env" / "test.tfvars").write_text('name = "gke-jfrog-test"\njfrog_url = "https://acme.jfrog.io"\n')

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

    def test_remote_state_stack_uses_env_files(self):
        init, plan = stacks.tf_args("gke-cluster", "test", self.root.dir)
        self.assertEqual(init, ("-reconfigure", "-backend-config=env/test.backend.hcl"))
        self.assertEqual(plan, ("-var-file=env/test.tfvars",))

    def test_local_state_stack_uses_terraform_tfvars(self):
        self.assertEqual(stacks.tf_args("ado-trust", "test", self.root.dir), ((), ()))
        self.assertEqual(stacks.vars_file("ado-trust", "test", self.root.dir).name, "terraform.tfvars")

    def test_unknown_stack_refused(self):
        with self.assertRaises(stacks.StackError):
            stacks.stack_dir("../etc", self.root.dir)

    def test_inputs_never_hold_secrets(self):
        with self.assertRaises(stacks.StackError):
            stacks.write_vars("gke-cluster", "test", 'jfrog_access_token = "x"\n', self.root.dir)
        stacks.write_vars("gke-cluster", "test", 'name = "other"', self.root.dir)
        self.assertEqual(stacks.read_vars("gke-cluster", "test", self.root.dir), 'name = "other"\n')

    def test_jfrog_host_from_inputs(self):
        self.assertEqual(stacks.jfrog_host("gke-cluster", "test", self.root.dir), "acme.jfrog.io")
        self.assertIsNone(stacks.jfrog_host("aks-cluster", "test", self.root.dir))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(shutil.rmtree, self.root.dir)
        self.minted = []
        self.jobs = Jobs(root=self.root.dir, shell_factory=fake_shell, environ={},
                         token_source=lambda sh, host: self.minted.append(host) or "minted-token-value")

    def _plan(self, sid, changes, action="plan-apply"):
        (self.root.dir / sid / ".fake_plan.json").write_text(json.dumps(changes))
        return wait(self.jobs, self.jobs.start(sid, action))

    def test_deploy_needs_the_approved_plan(self):
        j = self._plan("gke-cluster", [{"address": "google_container_cluster.cluster", "type": "x", "change": {"actions": ["create"]}}])
        self.assertEqual(j["state"], "done", j)
        plan = j["result"]
        self.assertEqual(plan["summary"]["create"], 1)
        self.assertIn("-var-file=env/test.tfvars", self.root.calls("gke-cluster")[1])
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
        self.console = SetupServer(token=TOKEN, root=self.root.dir, log=open("/dev/null", "w"),
                                   jobs=Jobs(root=self.root.dir, shell_factory=fake_shell, environ={}))
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

    def test_vars_round_trip_and_plan(self):
        self.assertEqual(self.req("PUT", "/api/stacks/gke-cluster/vars", {"text": 'name = "x"\n'})[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/stacks/gke-cluster/vars")[1])["text"], 'name = "x"\n')
        st, body, _ = self.req("POST", "/api/stacks/gke-cluster/jobs", {"action": "plan-apply"})
        self.assertEqual(st, 202)
        self.assertEqual(self.req("POST", "/api/stacks/nope/jobs", {"action": "plan-apply"})[0], 400)


if __name__ == "__main__":
    unittest.main()
