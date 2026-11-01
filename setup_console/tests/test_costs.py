import json
import tempfile
import unittest
from pathlib import Path

from setup_console import costs, stacks


class FakeJobs:
    """Plans return a sha; applies and reads succeed. Records every call with the inputs seen then."""
    def __init__(self, state_root, env="test", gone=(), down=()):
        self.calls, self.state_root, self.env, self.gone, self.n = [], state_root, env, set(gone), 0
        self.down = set(down)

    def start(self, sid, action, approval=None):
        self.n += 1
        inputs = stacks.read_inputs(sid, self.env, self.state_root)
        self.calls.append((sid, action, approval, inputs))
        if action == "status":
            res = {"deployed": sid not in self.down}
        elif action == "apply":
            self.down.discard(sid)
            res = {"applied": True}
        elif action == "plan-destroy" and sid in self.gone:
            res = {"already_deleted": True}
        elif action.startswith("plan-"):
            res = {"sha256": f"{self.n:064x}", "summary": {"create": 1}}
        else:
            res = {"applied": True}
        return {"id": str(self.n), "state": "done", "result": res}

    def get(self, job_id):
        raise AssertionError("never polled: jobs finish at once")


class FakeAdo:
    def __init__(self, warnings):
        self.warnings, self.runs = list(warnings), []

    def pipeline(self, path):
        return 7, path

    def queue(self, pid, params):
        self.runs.append(params)
        return len(self.runs)

    def build(self, run):
        return {"state": "completed", "result": "succeeded", "errors": [],
                "warnings": self.warnings.pop(0) if self.warnings else []}

    def run_url(self, run):
        return f"run/{run}"


class CostsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lines = []

    def save(self, sid, values, complete=True):
        blank = {"string": "x", "number": 1, "bool": False, "list": [], "map": {}}
        for v in stacks.variables(sid) if complete else ():   # the stack's other required inputs, set
            if v["required"] and v["name"] not in values:
                values = {**values, v["name"]: {f: "x" for f in v.get("fields", ())} if v["kind"] == "object" else blank.get(v["kind"], "x")}
        p = stacks.inputs_file(sid, "test", self.tmp)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(values))

    def runner(self, jobs, ado):
        return costs.Runner(jobs, ado, "test", state_root=self.tmp, sleep=lambda s: None, out=self.lines.append)

    def test_only_costing_stacks_never_foundations_or_trust(self):
        for s in costs.select():
            self.assertNotIn(stacks.get(s)["kind"], ("JFrog OIDC",))
            self.assertIsNotNone(stacks.depends_on(s), s)   # foundations keep local state: never here
        self.assertEqual(costs.select("gcp", None)[-1], "gke-cluster")
        self.assertTrue(all(stacks.get(s)["group"] == "Azure" for s in costs.select("azure")))

    def test_down_approves_the_exact_plan_with_the_name_typed(self):
        jobs = FakeJobs(self.tmp, gone={"gke-cluster"})
        res = costs.run_all(self.runner(jobs, FakeAdo([])), "down", ["aks-cluster", "gke-cluster"])
        self.assertEqual(res, {"aks-cluster": "destroyed", "gke-cluster": "already deleted"})
        plan = next(c for c in jobs.calls if c[:2] == ("aks-cluster", "plan-destroy"))
        apply = next(c for c in jobs.calls if c[:2] == ("aks-cluster", "apply"))
        self.assertEqual(apply[2], {"plan_sha256": f"{jobs.calls.index(plan) + 1:064x}", "typed_confirmation": "aks-cluster"})
        self.assertFalse(any(c[:2] == ("gke-cluster", "apply") for c in jobs.calls))
        self.assertEqual([c[0] for c in jobs.calls if c[1] == "status"], ["azure-foundation", "gcp-foundation"])

    def test_up_brings_back_a_foundation_deleted_outside_with_its_jfrog_trust_first(self):
        self.save("gke-cluster", {"node_count": 1})
        jobs = FakeJobs(self.tmp, down={"gcp-foundation"})
        costs.run_all(self.runner(jobs, FakeAdo([])), "up", ["gke-cluster"])
        order = [c[0] for c in jobs.calls if c[1] == "apply"]
        self.assertEqual(order, ["gcp-foundation", "gcp-jfrog-trust", "gke-cluster"])

    def test_down_goes_on_when_a_foundation_is_gone(self):
        jobs = FakeJobs(self.tmp, down={"azure-foundation"}, gone={"aks-cluster"})
        res = costs.run_all(self.runner(jobs, FakeAdo([])), "down", ["aks-cluster"])
        self.assertEqual(res, {"aks-cluster": "already deleted"})
        self.assertFalse(any(c[1] == "plan-apply" for c in jobs.calls))

    def test_up_bootstraps_a_workload_whose_registry_is_new(self):
        sid = "azure-container-app"
        self.save(sid, {"workload": "aca-demo", "deploy_app": True, "app_image": "aca-demo:old"})
        ado = FakeAdo([["Container App aca-demo does not exist yet: pushed aca-demo:abc123. In Setup, set app_image "
                        "to it and deploy_app on, deploy, then run this pipeline again."], []])
        jobs = FakeJobs(self.tmp)
        self.assertEqual(self.runner(jobs, ado).up(sid), "deployed and rolled out")
        plans = [c[3] for c in jobs.calls if c[1] == "plan-apply"]
        self.assertFalse(plans[0]["deploy_app"])
        self.assertEqual((plans[1]["deploy_app"], plans[1]["app_image"]), (True, "aca-demo:abc123"))
        self.assertEqual(stacks.read_inputs(sid, "test", self.tmp)["app_image"], "aca-demo:abc123")
        self.assertEqual(ado.runs, [{"environment": "test", "workload": "aca-demo"}] * 2)

    def test_up_restores_inputs_when_the_first_pass_fails(self):
        sid = "gcp-cloud-run-service"
        self.save(sid, {"workload": "svc", "deploy_service": True, "service_image": "img:1"})
        saved = stacks.read_inputs(sid, "test", self.tmp)

        class Failing(FakeAdo):
            def build(self, run):
                return {"state": "completed", "result": "failed", "errors": ["boom"], "warnings": []}
        with self.assertRaises(costs.CostError):
            self.runner(FakeJobs(self.tmp), Failing([])).up(sid)
        self.assertEqual(stacks.read_inputs(sid, "test", self.tmp), saved)

    def test_up_without_a_switch_deploys_then_rolls_out(self):
        self.save("azure-functions-premium", {"workload": "fp", "plan_sku": "EP1"})
        ado, jobs = FakeAdo([]), FakeJobs(self.tmp)
        self.runner(jobs, ado).up("azure-functions-premium")
        self.assertEqual([c[1] for c in jobs.calls], ["plan-apply", "apply"])
        self.assertEqual(len(ado.runs), 1)

    def test_up_stops_before_any_run_when_an_input_is_missing(self):
        self.save("aks-cluster", {"azure_subscription_id": "sub", "node_count": 1}, complete=False)
        jobs = FakeJobs(self.tmp)
        with self.assertRaisesRegex(costs.CostError, "argocd_identity"):
            self.runner(jobs, FakeAdo([])).up("aks-cluster")
        self.assertEqual(jobs.calls, [])

    def test_the_clusters_jfrog_trust_comes_up_after_it_and_goes_down_before_it(self):
        self.save("aks-cluster", {"node_count": 1})
        self.save("aks-jfrog-trust", {"cluster_name": "c"})
        jobs = FakeJobs(self.tmp)
        self.runner(jobs, FakeAdo([])).up("aks-cluster")
        self.assertEqual([c[0] for c in jobs.calls if c[1] == "apply"], ["aks-cluster", "aks-jfrog-trust"])
        jobs = FakeJobs(self.tmp)
        costs.run_all(self.runner(jobs, FakeAdo([])), "down", ["aks-cluster"])
        self.assertEqual([c[0] for c in jobs.calls if c[1] == "apply"], ["aks-jfrog-trust", "aks-cluster"])

    def test_up_stops_when_the_clusters_jfrog_trust_is_not_filled_in(self):
        self.save("aks-cluster", {"node_count": 1})
        jobs = FakeJobs(self.tmp)
        with self.assertRaisesRegex(costs.CostError, "aks-jfrog-trust"):
            self.runner(jobs, FakeAdo([])).up("aks-cluster")
        self.assertEqual(jobs.calls, [])

    def test_a_cluster_has_no_build_deploy(self):
        self.save("gke-cluster", {"node_count": 1})
        ado = FakeAdo([])
        self.runner(FakeJobs(self.tmp), ado).up("gke-cluster")
        self.assertEqual(ado.runs, [])


if __name__ == "__main__":
    unittest.main()
