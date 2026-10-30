"""Delivery through Azure DevOps: publish renders the workspace into a branch and opens a PR;
wire_workspace wires every target after the pipeline applied. Replayed git/az/HTTP."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from console import deliver, model
from console.shell import Result, Shell
from console.steps import tfgen

P, R = "demo-nonprod-1234", "us-central1"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.ado-access-token-value.signature"
ORG = "https://dev.azure.com/acme"
REPO_URL = f"{ORG}/platform/_git/serverless"
DELIVERY = {
    "ado": {"organization": "acme", "project": "platform", "repository": "serverless", "target_branch": "main"},
    "state": {"gcp": {"type": "gcs", "bucket": "acme-tfstate"},
              "azure": {"type": "azurerm", "resource_group_name": "rg-ci", "storage_account_name": "stacmetfstate",
                        "container_name": "tfstate"}},
}
WS = "terraform/workspaces/gcp_demo-nonprod-1234_us-central1"


def gcp_cfg(name="orders"):
    c = model.default_config("gcp_run_service", name=name)
    c["target"]["gcp"].update(project=P, region=R, name=name)
    c["jfrog"]["url"] = "https://acme.jfrog.io"
    c["pull_path"]["enabled"] = False
    c["target"]["discovered"] = {"kind": "gcp_run_service", "mapping_subject": "104857600000000000001"}
    return c


class FakeGit(Shell):
    """Replay az/git/HTTP for publish. Temp paths vary, so commands are matched by their verb."""

    def __init__(self, log=None, commit_rc=0, pr_status=201):
        super().__init__(mode="replay", log=log)
        self.calls, self.commit_rc, self.pr_status = [], commit_rc, pr_status

    def _replay_cmd(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["az", "account", "get-access-token"]:
            return Result(argv, 0, TOKEN + "\n", "", 0.0)
        if argv[0] == "git":
            verb = argv[3]
            if verb == "commit":
                return Result(argv, self.commit_rc, "nothing to commit" if self.commit_rc else "", "", 0.0)
            if verb == "diff":   # `git diff --cached --quiet`: 1 = staged changes
                return Result(argv, 0 if self.commit_rc else 1, "", "", 0.0)
            return Result(argv, 0, "", "", 0.0)
        self.misses.append(" ".join(argv))
        return Result(argv, 127, "", "no fixture", 0.0)

    def _replay_http(self, method, url):
        from console.shell import HttpResult
        self.calls.append([method, url])
        if url == f"{ORG}/platform/_apis/git/repositories/serverless/pullrequests?api-version=7.1":
            return HttpResult(method, url, self.pr_status, json.dumps({"pullRequestId": 42}))
        self.misses.append(f"{method} {url}")
        return HttpResult(method, url, 0, "", "no fixture")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="deliver-repo-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.lines = []

    def publish(self, cfg, run_id="0123456789abcdef", **kw):
        sh = FakeGit(self.lines.append, **kw)
        res = deliver.publish(cfg, sh, self.repo, DELIVERY, run_id, log=self.lines.append)
        return res, sh

    def verbs(self, sh):
        return [c[3] if c[0] == "git" else c[0] for c in sh.calls]

    def test_opens_a_pull_request_with_the_rendered_workspace(self):
        res, sh = self.publish(gcp_cfg())
        self.assertEqual(sh.misses, [])
        self.assertTrue(res["changed"])
        self.assertEqual(res["pull_request"], {"id": 42, "url": f"{REPO_URL}/pullrequest/42"})
        self.assertEqual(res["branch"], "onboard/orders-01234567")
        self.assertEqual(res["workspace"], WS)
        self.assertEqual(self.verbs(sh), ["az", "fetch", "worktree", "add", "diff", "commit", "push", "POST",
                                          "worktree", "branch"])
        fetch = sh.calls[1]
        self.assertEqual(fetch[4:], ["--quiet", REPO_URL, "+refs/heads/main:refs/remotes/delivery/main"])
        add = next(c for c in sh.calls if c[0] == "git" and c[3] == "add")
        self.assertEqual(sorted(add[add.index("--") + 1:]), sorted(f"{WS}/{n}" for n in (
            "backend.tf.json", "console.json", "main.tf.json", "terraform.tfvars.json", "configs/orders.json")))
        push = next(c for c in sh.calls if c[0] == "git" and c[3] == "push")
        self.assertEqual(push[4:], ["--quiet", REPO_URL, "onboard/orders-01234567:refs/heads/onboard/orders-01234567"])

    def test_workspace_uses_relative_modules_and_remote_state(self):
        seen = {}
        orig = deliver._render

        def spy(cfg, wt):
            out = orig(cfg, wt)
            ws = Path(wt) / out["workspace"]
            seen["main"] = json.loads((ws / "main.tf.json").read_text())
            seen["backend"] = json.loads((ws / "backend.tf.json").read_text())
            seen["config"] = json.loads((ws / "configs" / "orders.json").read_text())
            return out
        deliver._render = spy
        self.addCleanup(setattr, deliver, "_render", orig)
        self.publish(gcp_cfg())
        sources = [m["source"] for m in seen["main"]["module"].values()]
        self.assertTrue(sources and all(s.startswith("../../modules/") for s in sources), sources)
        self.assertEqual(seen["backend"]["terraform"]["backend"],
                         {"gcs": {"bucket": "acme-tfstate", "prefix": "workspaces/gcp_demo-nonprod-1234_us-central1"}})
        self.assertEqual(seen["config"]["name"], "orders")

    def test_no_change_means_no_pull_request(self):
        res, sh = self.publish(gcp_cfg(), commit_rc=1)
        self.assertFalse(res["changed"])
        self.assertIsNone(res["pull_request"])
        self.assertNotIn("push", self.verbs(sh))
        self.assertEqual(self.verbs(sh)[-2:], ["worktree", "branch"])

    def test_pull_request_failure_is_an_error_and_cleans_up(self):
        sh = FakeGit(None, pr_status=403)
        with self.assertRaises(deliver.DeliveryError):
            deliver.publish(gcp_cfg(), sh, self.repo, DELIVERY, "0123456789abcdef")
        self.assertEqual(self.verbs(sh)[-2:], ["worktree", "branch"])

    def test_token_never_logged_or_returned(self):
        res, _ = self.publish(gcp_cfg())
        self.assertNotIn(TOKEN, json.dumps(res))
        self.assertFalse(any(TOKEN in line for line in self.lines))

    def test_local_backend_settings_are_refused(self):
        bad = {**DELIVERY, "state": {"gcp": {"type": "local"}}}
        with self.assertRaises(deliver.DeliveryError):
            deliver.publish(gcp_cfg(), FakeGit(), self.repo, bad, "0123456789abcdef")


class LoadTests(unittest.TestCase):
    def test_load_settings(self):
        d = Path(tempfile.mkdtemp(prefix="deliver-cfg-"))
        self.addCleanup(shutil.rmtree, d, True)
        self.assertIsNone(deliver.load_settings(d / "missing.json"))
        (d / "delivery.json").write_text(json.dumps(DELIVERY))
        self.assertEqual(deliver.load_settings(d / "delivery.json")["ado"]["project"], "platform")
        (d / "bad.json").write_text(json.dumps({"ado": {"organization": "acme"}}))
        with self.assertRaises(deliver.DeliveryError):
            deliver.load_settings(d / "bad.json")


class WireWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="deliver-ws-"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        (self.ws / "configs").mkdir()

    def test_wires_every_config_with_its_own_plan_hash(self):
        for n in ("orders", "billing"):
            (self.ws / "configs" / f"{n}.json").write_text(json.dumps(gcp_cfg(n)))
        seen = []

        class FakeWire:
            def __init__(self, cfg, sh, outputs):
                self.name, self.outputs = cfg["name"], outputs

            def plan(self):
                return {"noop": self.name == "billing", "sha256": f"sha-{self.name}", "diff": [], "target": self.name}

            def apply(self, plan, approval):
                seen.append((self.name, approval["diff_sha256"], self.outputs))
                return {"verified": True}
        res = deliver.wire_workspace(self.ws, Shell(mode="replay"), {"gcp_provider_name": "gcp-cloudrun"}, wire_cls=FakeWire)
        self.assertEqual(seen, [("orders", "sha-orders", {"gcp_provider_name": "gcp-cloudrun"})])
        self.assertEqual({r["name"]: r["status"] for r in res}, {"billing": "already wired", "orders": "wired"})

    def test_unverified_wire_fails(self):
        (self.ws / "configs" / "orders.json").write_text(json.dumps(gcp_cfg()))

        class Bad:
            def __init__(self, *a):
                pass

            def plan(self):
                return {"noop": False, "sha256": "x", "diff": [], "target": "orders"}

            def apply(self, plan, approval):
                return {"verified": False}
        with self.assertRaises(deliver.DeliveryError):
            deliver.wire_workspace(self.ws, Shell(mode="replay"), {}, wire_cls=Bad)


class TfgenModulesTests(unittest.TestCase):
    def test_default_sources_stay_absolute(self):
        root = Path(tempfile.mkdtemp(prefix="deliver-tf-"))
        self.addCleanup(shutil.rmtree, root, True)
        out = tfgen.render(gcp_cfg(), root)
        main = json.loads((Path(out["dir"]) / "main.tf.json").read_text())
        self.assertTrue(all(Path(m["source"]).is_absolute() for m in main["module"].values()))


class RunnerDeliveryTests(unittest.TestCase):
    def setUp(self):
        import time as _t
        self._t = _t
        from console.runner import RunManager
        self.root = Path(tempfile.mkdtemp(prefix="deliver-run-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.mgr = RunManager(self.root / ".console", cloud_shell=lambda log: FakeGit(log),
                              tf_shell=lambda log: Shell(mode="replay", log=log),
                              env={"PATH": "/usr/bin:/bin"}, delivery=DELIVERY, repo_root=self.root)
        self.addCleanup(self.mgr.shutdown)

    def wait(self, run):
        for _ in range(200):
            if run.status not in ("running", "waiting_approval"):
                return run.snapshot()
            self._t.sleep(0.02)
        self.fail("run did not finish")

    def test_publish_opens_a_pr_and_hides_the_local_apply_steps(self):
        snap = self.wait(self.mgr.start(gcp_cfg(), steps=["publish"]))
        steps = {s["name"]: s for s in snap["steps"]}
        self.assertEqual(snap["status"], "succeeded", steps["publish"])
        self.assertEqual(steps["publish"]["evidence"]["result"]["pull_request"]["url"], f"{REPO_URL}/pullrequest/42")
        for s in ("plan", "apply", "wire"):
            self.assertTrue(steps[s]["planned"], s)

    def test_local_steps_are_refused_in_delivery_mode(self):
        from console.runner import RunError
        with self.assertRaises(RunError) as ctx:
            self.mgr.start(gcp_cfg(), steps=["plan", "apply"])
        self.assertEqual(ctx.exception.code, "delivered_by_pipeline")

    def test_default_steps_end_with_publish(self):
        from console.runner import RunManager
        self.assertEqual(self.mgr.default_steps(), ["preflight", "discover", "simulate", "publish"])
        local = RunManager(self.root / "local", cloud_shell=None, tf_shell=None, env={})
        self.assertNotIn("publish", local.default_steps())
        with self.assertRaises(Exception):
            local.start(gcp_cfg(), steps=["publish"])


if __name__ == "__main__":
    unittest.main()
