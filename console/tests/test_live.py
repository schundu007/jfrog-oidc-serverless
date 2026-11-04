import json
import unittest

from console import live
from console.shell import Shell


def fx(argv, out, rc=0):
    return {"argv": argv, "rc": rc, "stdout": json.dumps(out)}


def run_item(name, env, region="us-central1", job=False):
    spec = {"containers": [{"env": [{"name": k, "value": "v"} for k in env]}]}
    if job:
        spec = {"template": {"spec": spec}}
    return {"metadata": {"name": name, "labels": {"cloud.googleapis.com/location": region}},
            "spec": {"template": {"spec": spec}}}


def gcp_cfg(name, kind, target=None, run_service=None):
    return {"name": name, "target": {"kind": kind, "gcp": {"project": "p1", "region": "us-central1", "name": target or name},
                                     "discovered": {"run_service": run_service} if run_service else {}}}


class GcpLive(unittest.TestCase):
    def test_wired_not_wired_missing(self):
        sh = Shell(mode="replay", fixtures=[
            fx(["gcloud", "run", "jobs", "list", "--project", "p1", "--format=json"],
               [run_item("job-a", ["JFROG_OIDC_PROVIDER"], job=True), run_item("job-b", ["OTHER"], job=True)]),
            fx(["gcloud", "run", "services", "list", "--project", "p1", "--format=json"],
               [run_item("fn-svc", ["JFROG_OIDC_PROVIDER"])]),
        ])
        got = live.status("gcp", [gcp_cfg("a", "gcp_run_job", "job-a"), gcp_cfg("b", "gcp_run_job", "job-b"),
                                  gcp_cfg("gone", "gcp_run_service"),
                                  gcp_cfg("fn", "gcp_run_function", "fn", run_service="fn-svc")], sh)
        self.assertEqual({k: v["state"] for k, v in got.items()},
                         {"a": "wired", "b": "not_wired", "gone": "missing", "fn": "wired"})

    def test_worker_pool_from_the_beta_group_in_the_v2_shape(self):
        pool = {"name": "projects/p1/locations/us-central1/workerPools/consumer",
                "template": {"containers": [{"env": [{"name": "JFROG_OIDC_PROVIDER", "value": "v"}]}]}}
        sh = Shell(mode="replay", fixtures=[
            fx(["gcloud", "run", "services", "list", "--project", "p1", "--format=json"], []),
            fx(["gcloud", "run", "jobs", "list", "--project", "p1", "--format=json"], []),
            fx(["gcloud", "beta", "run", "worker-pools", "list", "--project", "p1", "--format=json"], [pool]),
        ])
        got = live.status("gcp", [gcp_cfg("consumer", "gcp_run_worker_pool"), gcp_cfg("other", "gcp_run_worker_pool")], sh)
        self.assertEqual({k: v["state"] for k, v in got.items()}, {"consumer": "wired", "other": "missing"})

    def test_read_failure_is_unknown_not_missing(self):
        sh = Shell(mode="replay", fixtures=[fx(["gcloud", "run", "jobs", "list", "--project", "p1", "--format=json"], [], rc=1)])
        got = live.status("gcp", [gcp_cfg("a", "gcp_run_job")], sh)
        self.assertEqual(got["a"]["state"], "unknown")


class AzureLive(unittest.TestCase):
    def test_container_app_and_function_app(self):
        app = {"name": "ca", "resourceGroup": "RG", "properties": {"template": {"containers": [{"env": [{"name": "JFROG_OIDC_PROVIDER"}]}]}}}
        sh = Shell(mode="replay", fixtures=[
            fx(["az", "containerapp", "list", "--subscription", "s", "-o", "json"], [app]),
            fx(["az", "functionapp", "list", "--subscription", "s", "-o", "json"], [{"name": "fn", "resourceGroup": "rg"}]),
            fx(["az", "functionapp", "config", "appsettings", "list", "-g", "rg", "-n", "fn", "--subscription", "s", "-o", "json"],
               [{"name": "AzureWebJobsStorage", "value": "secret"}]),
        ])
        cfg = lambda n, kind, name: {"name": n, "target": {"kind": kind, "azure": {"subscription": "s", "resource_group": "rg", "name": name}}}
        got = live.status("azure", [cfg("a", "azure_container_app", "ca"), cfg("f", "azure_functions_premium", "fn"),
                                    cfg("gone", "azure_container_app", "nope")], sh)
        self.assertEqual({k: v["state"] for k, v in got.items()}, {"a": "wired", "f": "not_wired", "gone": "missing"})
        self.assertNotIn("secret", json.dumps(got))

    def test_web_app(self):
        sh = Shell(mode="replay", fixtures=[
            fx(["az", "webapp", "list", "--subscription", "s", "-o", "json"], [{"name": "web", "resourceGroup": "rg"}]),
            fx(["az", "webapp", "config", "appsettings", "list", "-g", "rg", "-n", "web", "--subscription", "s", "-o", "json"],
               [{"name": "JFROG_OIDC_PROVIDER", "value": "p"}]),
        ])
        cfg = {"name": "w", "target": {"kind": "azure_web_app", "azure": {"subscription": "s", "resource_group": "rg", "name": "web"}}}
        self.assertEqual(live.status("azure", [cfg], sh)["w"]["state"], "wired")


if __name__ == "__main__":
    unittest.main()
