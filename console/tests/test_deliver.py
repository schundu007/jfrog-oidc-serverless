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
    c["target"]["discovered"] = {"kind": "gcp_run_service", "mapping_subject": "104857600000000000001"}
    return c


class FakeGit(Shell):
    """Replay az/git/HTTP for publish. Temp paths vary, so commands are matched by their verb."""

    def __init__(self, log=None, commit_rc=0, pr_status=201, helper="", token_ok=True, helper_ok=True,
                 helper_error="", helper_push_ok=True, onboard_def=True):
        super().__init__(mode="replay", log=log)
        self.calls, self.commit_rc, self.pr_status, self.onboard_def, self.bodies = [], commit_rc, pr_status, onboard_def, []
        self.headers = []
        self.helper, self.token_ok, self.helper_ok, self.envs = helper, token_ok, helper_ok, []
        self.helper_error, self.helper_push_ok = helper_error, helper_push_ok

    def run(self, argv, *a, extra_env=None, **kw):
        self.envs.append(extra_env)
        return super().run(argv, *a, extra_env=extra_env, **kw)

    def _replay_cmd(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["az", "account", "get-access-token"]:
            return Result(argv, 0, TOKEN + "\n", "", 0.0)
        if argv[0] == "git":
            verb = argv[3]
            if verb == "config":   # `git config --get-urlmatch credential.helper <org>`
                return Result(argv, 0 if self.helper else 1, self.helper + "\n" if self.helper else "", "", 0.0)
            if verb in ("fetch", "push"):
                env = self.envs[-1] or {}
                ok = self.token_ok if "GIT_CONFIG_VALUE_0" in env else (self.helper_ok and (verb == "fetch" or self.helper_push_ok))
                if not ok:
                    return Result(argv, 128, "", self.helper_error if "GIT_CONFIG_VALUE_0" not in env and self.helper_error
                                  else "fatal: Authentication failed for 'https://dev.azure.com/acme/'", 0.0)
            if verb == "commit":
                return Result(argv, self.commit_rc, "nothing to commit" if self.commit_rc else "", "", 0.0)
            if verb == "diff":   # `git diff --cached --quiet`: 1 = staged changes
                return Result(argv, 0 if self.commit_rc else 1, "", "", 0.0)
            return Result(argv, 0, "", "", 0.0)
        self.misses.append(" ".join(argv))
        return Result(argv, 127, "", "no fixture", 0.0)

    def http(self, method, url, *a, body=None, headers=None, **kw):
        self.headers.append(dict(headers or {}))
        if body:
            self.bodies.append(json.loads(body))
        return super().http(method, url, *a, body=body, headers=headers, **kw)

    def _replay_http(self, method, url):
        from console.shell import HttpResult
        self.calls.append([method, url])
        if url == f"{ORG}/platform/_apis/git/repositories/serverless/pullrequests?api-version=7.1":
            return HttpResult(method, url, self.pr_status, json.dumps({"pullRequestId": 42}))
        if url.startswith(f"{ORG}/platform/_apis/build/definitions?"):
            defs = [{"id": 3, "process": {"yamlFilename": "pipelines/serverless/gcp/gcp-cloud-run/build-deploy.yml"}}]
            if self.onboard_def:
                defs.append({"id": 12, "process": {"yamlFilename": "/pipelines/gcp-workload-onboard.yml"}})
            return HttpResult(method, url, 200, json.dumps({"value": defs}))
        if url == f"{ORG}/platform/_apis/pipelines/12/runs?api-version=7.1":
            return HttpResult(method, url, 200, json.dumps({"id": 77, "state": "inProgress"}))
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
        self.assertEqual(self.verbs(sh), ["az", "config", "fetch", "worktree", "add", "diff", "commit", "push", "POST",
                                          "worktree", "branch"])
        fetch = sh.calls[2]
        self.assertEqual(fetch[4:], ["--quiet", REPO_URL, "+refs/heads/main:refs/remotes/delivery/main"])
        add = next(c for c in sh.calls if c[0] == "git" and c[3] == "add")
        self.assertEqual(sorted(add[add.index("--") + 1:]), sorted(f"{WS}/{n}" for n in (
            "backend.tf.json", "console.json", "main.tf.json", "terraform.tfvars.json", "configs/orders.json")))
        push = next(c for c in sh.calls if c[0] == "git" and c[3] == "push")
        self.assertEqual(push[4:], ["--quiet", REPO_URL, "onboard/orders-01234567:refs/heads/onboard/orders-01234567"])

    def test_a_target_whose_identity_is_gone_is_taken_out_of_the_workspace(self):
        from console.tests.test_tfgen import azure_cfg
        GONE, LIVE = "aaaaaaaa-0000-0000-0000-00000000dead", "aaaaaaaa-0000-0000-0000-00000000beef"
        wt = self.repo / ".console" / "delivery" / "0123456789abcdef"
        old = azure_cfg(name="old-app", principal=GONE)
        old["jfrog"]["url"] = "https://acme.jfrog.io"
        old["state_backend"] = deliver._backend(DELIVERY, "azure", "x")
        first = deliver._render(old, wt)
        cfg = azure_cfg(name="web", principal=LIVE)
        cfg["jfrog"]["url"] = "https://acme.jfrog.io"

        class Sp(FakeGit):
            def _replay_cmd(self, argv):
                if argv[:4] == ["az", "ad", "sp", "show"]:
                    self.calls.append(list(argv))
                    return Result(argv, 3, "", f"ERROR: Resource '{argv[5]}' does not exist or one of its queried "
                                                "reference-property objects are not present.", 0.0)
                return super()._replay_cmd(argv)
        sh = Sp(self.lines.append)
        res = deliver.publish(cfg, sh, self.repo, DELIVERY, "0123456789abcdef", log=self.lines.append)
        self.assertEqual(sh.misses, [])
        ws = wt / first["workspace"]
        state = json.loads((ws / "console.json").read_text())
        self.assertEqual(sorted(state["targets"]), ["web"])
        self.assertEqual(state["shared"]["resource_group_name"], "rg-apps")
        self.assertFalse((ws / "configs" / "old-app.json").exists())
        rm = next(c for c in sh.calls if c[0] == "git" and c[3] == "rm")
        self.assertEqual(rm[-1], f"{first['workspace']}/configs/old-app.json")
        self.assertTrue(any("old-app no longer exists" in l for l in self.lines))
        self.assertTrue(res["changed"])

    def test_a_target_is_kept_when_the_identity_check_fails_otherwise(self):
        from console.tests.test_tfgen import azure_cfg
        wt = self.repo / ".console" / "delivery" / "x"
        old = azure_cfg(name="old-app", principal="aaaaaaaa-0000-0000-0000-00000000dead")
        old["jfrog"]["url"] = "https://acme.jfrog.io"
        deliver._render(old, wt)
        cfg = azure_cfg(name="web", principal="aaaaaaaa-0000-0000-0000-00000000beef")

        class Down(Shell):
            def __init__(self):
                super().__init__(mode="replay")

            def _replay_cmd(self, argv):
                return Result(argv, 1, "", "ERROR: network unreachable", 0.0)
        self.assertEqual(deliver.gone_targets(cfg, wt, Down()), [])

    def test_workspace_uses_relative_modules_and_remote_state(self):
        seen = {}
        orig = deliver._render

        def spy(cfg, wt, drop=()):
            out = orig(cfg, wt, drop)
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

    def test_direct_merge_pushes_to_main_without_a_pull_request(self):
        sh = FakeGit(self.lines.append)
        res = deliver.publish(gcp_cfg(), sh, self.repo, {**DELIVERY, "merge": "direct"}, "0123456789abcdef",
                              log=self.lines.append)
        self.assertEqual(sh.misses, [])
        self.assertIsNone(res["pull_request"])
        self.assertEqual(res["branch"], "main")
        self.assertEqual(res["pipeline"]["name"], "Google Cloud - Onboard workloads to JFrog")
        self.assertNotIn("POST", self.verbs(sh))
        push = next(c for c in sh.calls if c[0] == "git" and c[3] == "push")
        self.assertEqual(push[4:], ["--quiet", REPO_URL, "onboard/orders-01234567:refs/heads/main"])

    def test_azure_state_uses_entra_auth(self):
        from console.steps import tfgen as tg
        c = model.default_config("azure_container_app", name="api")
        c["state_backend"] = deliver._backend(DELIVERY, "azure", "azure_x")
        self.assertEqual(tg._backend(c)["azurerm"]["use_azuread_auth"], True)

    def test_openshift_state(self):
        from console.steps import tfgen as tg
        d = {**DELIVERY, "state": {"openshift": {"type": "kubernetes", "namespace": "terraform", "config_context": "ci"}}}
        c = model.default_config("openshift_knative_service", name="api")
        c["state_backend"] = deliver._backend(d, "openshift", "openshift_lab")
        self.assertEqual(tg._backend(c), {"kubernetes": {"secret_suffix": "openshift-lab", "namespace": "terraform",
                                                         "config_context": "ci"}})
        with self.assertRaises(deliver.DeliveryError):      # the wrong backend type for the cloud
            deliver._backend({"state": {"openshift": {"type": "gcs", "bucket": "x"}}}, "openshift", "openshift_1")

    def test_pipeline_login_replaces_local_context(self):
        k = model.default_config("openshift_knative_service", name="api")
        self.assertEqual(deliver.pipeline_target(k, {"KUBE_CONTEXT": "ci"})["target"]["openshift"]["context"], "ci")

    def test_no_change_means_no_pull_request(self):
        res, sh = self.publish(gcp_cfg(), commit_rc=1)
        self.assertFalse(res["changed"])
        self.assertIsNone(res["pull_request"])
        self.assertNotIn("push", self.verbs(sh))
        self.assertEqual(self.verbs(sh)[-2:], ["worktree", "branch"])

    def test_no_change_still_runs_onboard_for_that_workspace(self):
        # The workspace is already on main (an infra redeploy wiped the app's settings): Deliver
        # queues the onboard pipeline for it, so the app is applied and wired again.
        res, sh = self.publish(gcp_cfg(), commit_rc=1)
        self.assertEqual(res["pipeline"]["run"], 77)
        self.assertEqual(res["pipeline"]["url"], f"{ORG}/platform/_build/results?buildId=77")
        post = [c for c in sh.calls if c[0] == "POST"]
        self.assertEqual(post, [["POST", f"{ORG}/platform/_apis/pipelines/12/runs?api-version=7.1"]])
        self.assertEqual(sh.bodies[-1]["templateParameters"], {"workspace": Path(res["workspace"]).name})
        self.assertTrue(any("queued" in line and "77" in line for line in self.lines), self.lines)

    def test_ado_calls_send_only_the_bearer_token(self):
        # With X-VSS-ForceMsaPassThrough ADO answers 302/401 to a valid token (live, jfrog-cp-azure).
        for rc in (0, 1):   # pull request; onboard run
            _, sh = self.publish(gcp_cfg(), commit_rc=rc)
            self.assertTrue(sh.headers)
            for hd in sh.headers:
                self.assertNotIn("X-VSS-ForceMsaPassThrough", hd)

    def test_no_change_without_an_onboard_pipeline_says_what_to_do(self):
        with self.assertRaisesRegex(deliver.DeliveryError, r"gcp-workload-onboard.*register"):
            self.publish(gcp_cfg(), commit_rc=1, onboard_def=False)

    def test_pull_request_failure_is_an_error_and_cleans_up(self):
        sh = FakeGit(None, pr_status=403)
        with self.assertRaises(deliver.DeliveryError):
            deliver.publish(gcp_cfg(), sh, self.repo, DELIVERY, "0123456789abcdef")
        self.assertEqual(self.verbs(sh)[-2:], ["worktree", "branch"])

    def test_token_never_logged_or_returned(self):
        res, _ = self.publish(gcp_cfg())
        self.assertNotIn(TOKEN, json.dumps(res))
        self.assertFalse(any(TOKEN in line for line in self.lines))

    def test_git_credential_helper_is_used_first(self):
        # Git Credential Manager holds the operator's ADO sign-in; the az token can be stale for MSA accounts.
        res, sh = self.publish(gcp_cfg(), helper="/opt/gcm/git-credential-manager")
        self.assertTrue(res["changed"])
        fetch_env = sh.envs[[i for i, c in enumerate(sh.calls) if c[0] == "git" and c[3] == "fetch"][0]]
        self.assertNotIn("GIT_CONFIG_VALUE_0", fetch_env)
        self.assertEqual(fetch_env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(fetch_env["GCM_INTERACTIVE"], "never")   # no sign-in window nobody answers

    def test_any_helper_failure_falls_back_and_the_token_goes_alone(self):
        # GCM without interactivity says it cannot prompt, or the call times out: still try the az token.
        res, sh = self.publish(gcp_cfg(), helper="/opt/gcm/git-credential-manager", helper_ok=False,
                               helper_error="fatal: Cannot prompt because user interactivity has been disabled.")
        self.assertTrue(res["changed"])
        push_env = sh.envs[[i for i, c in enumerate(sh.calls) if c[0] == "git" and c[3] == "push"][0]]
        self.assertEqual(push_env["GIT_CONFIG_COUNT"], "1")     # only the bearer header (ADO 401s with ForceMsaPassThrough)

    def test_falls_back_to_the_az_token_when_the_helper_fails(self):
        res, sh = self.publish(gcp_cfg(), helper="/opt/gcm/git-credential-manager", helper_ok=False)
        self.assertTrue(res["changed"])
        self.assertEqual([c[3] for c in sh.calls if c[0] == "git" and c[3] == "fetch"], ["fetch", "fetch"])
        push_env = sh.envs[[i for i, c in enumerate(sh.calls) if c[0] == "git" and c[3] == "push"][0]]
        self.assertIn("GIT_CONFIG_VALUE_0", push_env)      # the sign-in that worked is kept for the push

    def test_a_sign_in_that_only_reads_still_falls_back_for_the_push(self):
        # A public repo fetches with any helper; the push is where the helper's missing sign-in shows.
        res, sh = self.publish(gcp_cfg(), helper="/opt/gcm/git-credential-manager", helper_push_ok=False)
        self.assertTrue(res["changed"])
        pushes = [sh.envs[i] for i, c in enumerate(sh.calls) if c[0] == "git" and c[3] == "push"]
        self.assertEqual(["GIT_CONFIG_VALUE_0" in e for e in pushes], [False, True])

    def test_every_sign_in_failing_names_each_and_the_fix(self):
        with self.assertRaises(deliver.DeliveryError) as cm:
            self.publish(gcp_cfg(), helper="/opt/gcm/git-credential-manager", helper_ok=False, token_ok=False)
        msg = str(cm.exception)
        self.assertIn("git credential helper", msg)
        self.assertIn("az token", msg)
        self.assertIn("git push https://dev.azure.com/acme/platform/_git/serverless", msg)
        self.assertNotIn(TOKEN, msg)

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
        # Shipped neutral (no organization or project): local mode until Settings has them.
        (d / "neutral.json").write_text(json.dumps({"ado": {"repository": "r", "target_branch": "main"}}))
        self.assertIsNone(deliver.load_settings(d / "neutral.json"))
        settings = d / "settings.json"
        settings.write_text(json.dumps({"ado": {"organization": "acme", "project": "p", "repository": "r"}}))
        self.assertEqual(deliver.load_settings(d / "neutral.json", overrides=settings)["ado"]["organization"], "acme")
        (d / "bad.json").write_text(json.dumps({"ado": {"organization": "acme"}}))
        with self.assertRaises(deliver.DeliveryError):
            deliver.load_settings(d / "bad.json")

    def test_identity_and_state_come_from_the_pipeline_identity_stacks(self):
        d = Path(tempfile.mkdtemp(prefix="deliver-cfg-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "delivery.json").write_text(json.dumps({"ado": DELIVERY["ado"]}))
        stacks = d / "stacks"
        for name, outs in (("serverless/gcp/gcp-pipeline-identity", {"tfstate_bucket": "p-tfstate", "gcp_service_accounts": {"setup": "ado-setup@p.iam"}}),
                           ("serverless/azure/azure-pipeline-identity", {"azure_setup_identity_principal_id": "pid",
                                                 "azure_tfstate": {"resource_group": "rg", "storage_account": "st", "container": "tfstate"}})):
            (stacks / name).mkdir(parents=True)
            (stacks / name / "terraform.tfstate").write_text(json.dumps({"outputs": {k: {"value": v} for k, v in outs.items()}}))
        s = deliver.load_settings(d / "delivery.json", stacks)
        self.assertEqual(s["pipeline_identity"], {"gcp": "ado-setup@p.iam", "azure": "pid"})
        self.assertEqual(s["state"]["gcp"], {"type": "gcs", "bucket": "p-tfstate"})
        self.assertEqual(s["state"]["azure"]["storage_account_name"], "st")
        (stacks / "serverless/azure/azure-pipeline-identity" / "terraform.tfstate").unlink()    # not deployed: that cloud is left out
        self.assertNotIn("azure", deliver.load_settings(d / "delivery.json", stacks)["state"])

    def test_refresh_reads_a_pipeline_identity_deployed_after_start(self):
        d = Path(tempfile.mkdtemp(prefix="deliver-cfg-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "delivery.json").write_text(json.dumps({"ado": DELIVERY["ado"]}))
        stacks = d / "stacks"
        s = deliver.load_settings(d / "delivery.json", stacks)   # the console starts before the pipeline identity
        self.assertEqual(s["state"], {})
        f = stacks / "serverless/gcp/gcp-pipeline-identity"
        f.mkdir(parents=True)
        (f / "terraform.tfstate").write_text(json.dumps({"outputs": {"tfstate_bucket": {"value": "p-tfstate"},
                                                                     "gcp_service_accounts": {"value": {"setup": "ado-setup@p.iam"}}}}))
        deliver.refresh(s)
        self.assertEqual(s["state"]["gcp"], {"type": "gcs", "bucket": "p-tfstate"})
        self.assertEqual(s["pipeline_identity"]["gcp"], "ado-setup@p.iam")
        (f / "terraform.tfstate").unlink()   # torn down again: dropped, not kept from before
        deliver.refresh(s)
        self.assertNotIn("gcp", s["state"])
        deliver.refresh(None)   # local mode: nothing to refresh


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

    def test_a_target_that_no_longer_exists_is_skipped_and_the_rest_are_wired(self):
        for n in ("gone", "orders"):
            (self.ws / "configs" / f"{n}.json").write_text(json.dumps(gcp_cfg(n)))
        from console.steps.wire import TargetMissing, WireError

        class Partly:
            def __init__(self, cfg, sh, outputs):
                self.name = cfg["name"]

            def plan(self):
                if self.name == "gone":
                    raise TargetMissing("could not read the target: Cannot find service [gone]")
                return {"noop": False, "sha256": "x", "diff": [], "target": self.name}

            def apply(self, plan, approval):
                return {"verified": True}
        logs = []
        res = deliver.wire_workspace(self.ws, Shell(mode="replay"), {}, wire_cls=Partly, log=logs.append)
        self.assertEqual({r["name"]: r["status"] for r in res}, {"gone": "skipped: not found", "orders": "wired"})
        self.assertTrue(any("gone" in l and "not found" in l for l in logs))
        self.assertTrue(issubclass(TargetMissing, WireError))   # other read failures still fail the run


class HealthCheckTests(unittest.TestCase):
    """Post-deployment check in the onboarding pipeline: /healthz/jfrog on production and every slot."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="deliver-hc-"))
        self.addCleanup(shutil.rmtree, self.ws, True)
        (self.ws / "configs").mkdir()
        cfg = {"name": "web-a", "target": {"kind": "azure_web_app", "azure": {
            "name": "web-a", "resource_group": "rg-a", "subscription": "sub-1"}}}
        (self.ws / "configs" / "web-a.json").write_text(json.dumps(cfg))
        (self.ws / "configs" / "job-b.json").write_text(json.dumps(
            {"name": "job-b", "target": {"kind": "azure_container_app_job", "azure": {"name": "job-b"}}}))

    class Az(Shell):
        def __init__(self):
            super().__init__(mode="replay")
            self.calls = []

        def run(self, argv, *a, **kw):
            self.calls.append(argv)
            if "slot" in argv:
                return Result(argv, 0, json.dumps([{"name": "web-a/staging", "defaultHostName": "web-a-staging.azurewebsites.net"}]))
            return Result(argv, 0, json.dumps({"defaultHostName": "web-a.azurewebsites.net"}))

    def test_checks_production_and_every_slot_until_ok(self):
        got, answers = [], {"web-a.azurewebsites.net": [503, 200], "web-a-staging.azurewebsites.net": [200]}

        def fetch(url, timeout):
            host = url.split("/")[2]
            got.append(url)
            return answers[host].pop(0), {"ok": True, "username": "azure-entra/web-a"}
        res = deliver.check_workspace(self.ws, self.Az(), fetch=fetch, sleep=lambda s: None)
        self.assertEqual(res, [{"name": "web-a", "slot": "production", "status": 200},
                               {"name": "web-a", "slot": "staging", "status": 200}])
        self.assertTrue(all(u.endswith("/healthz/jfrog?fresh=1") and u.startswith("https://") for u in got))
        self.assertEqual(len(got), 3)   # production retried once

    def test_a_slot_that_never_gets_ok_fails_the_run(self):
        fetch = lambda url, timeout: (503, {"ok": False, "error": "Invalid audience"}) if "staging" in url else (200, {"ok": True})
        logs = []
        with self.assertRaisesRegex(deliver.DeliveryError, "staging"):
            deliver.check_workspace(self.ws, self.Az(), fetch=fetch, sleep=lambda s: None, tries=3, log=logs.append)
        self.assertTrue(any("Invalid audience" in l for l in logs))


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
                              env={"PATH": "/usr/bin:/bin"}, delivery=DELIVERY, repo_root=self.root,
                              order_check=lambda cfg, steps: None)
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

    def test_a_step_out_of_order_is_refused_before_the_run_starts(self):
        from console import order
        from console.runner import RunError, RunManager

        def busy(cfg, steps):
            raise order.OrderError("waiting on step 4 (Build and deploy): Web App - Build and deploy run 224 is inProgress")
        mgr = RunManager(self.root / ".console2", cloud_shell=lambda log: FakeGit(log),
                         tf_shell=lambda log: Shell(mode="replay", log=log), env={}, delivery=DELIVERY,
                         repo_root=self.root, order_check=busy)
        self.addCleanup(mgr.shutdown)
        with self.assertRaises(RunError) as ctx:
            mgr.start(gcp_cfg(), steps=["publish"])
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "out_of_order"))
        self.assertIn("step 4", str(ctx.exception))
        self.assertEqual(mgr.list(), [])

    def test_the_refusal_carries_its_next_step(self):
        from console import order
        from console.runner import RunError, RunManager

        def not_onboarded(cfg, steps):
            raise order.OrderError("waiting on step 5", next={"text": "Deliver first", "action": {"label": "Deliver now"}})
        mgr = RunManager(self.root / ".console4", cloud_shell=lambda log: FakeGit(log),
                         tf_shell=lambda log: Shell(mode="replay", log=log), env={}, delivery=DELIVERY,
                         repo_root=self.root, order_check=not_onboarded)
        self.addCleanup(mgr.shutdown)
        with self.assertRaises(RunError) as ctx:
            mgr.start(gcp_cfg(), steps=["prove"])
        self.assertEqual(ctx.exception.next["action"]["label"], "Deliver now")

    def test_the_default_order_check_reads_ado_only_with_delivery(self):
        from console.runner import RunManager
        calls = []
        local = RunManager(self.root / ".console3", cloud_shell=lambda log: calls.append(1),
                           tf_shell=lambda log: None, env={})
        self.addCleanup(local.shutdown)
        local.order_check(gcp_cfg(), ["prove"])
        self.assertEqual(calls, [])

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
