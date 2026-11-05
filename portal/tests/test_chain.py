"""Deploy on Infrastructure, step 5: the workload is onboarded already, or a Deliver run starts for it."""

import copy
import unittest

from portal import chain

DONE = {"name": "web-a", "jfrog": {"url": "https://acme.jfrog.io"}, "runtime": {"enabled": True},
        "target": {"kind": "azure_web_app", "azure": {"subscription": "s", "resource_group": "rg-web-a", "name": "web-a"},
                   "discovered": {"client_id": "c-1"}}}


class FakeStore:
    def __init__(self, saved):
        self.saved = {c["name"]: c for c in saved}

    def get(self, name):
        if name not in self.saved:
            raise FileNotFoundError(name)
        return copy.deepcopy(self.saved[name])

    def list(self):
        return [{"name": n, "kind": c["target"]["kind"]} for n, c in self.saved.items()]

    def put(self, name, cfg):
        self.saved[name] = dict(cfg, name=name)
        return self.saved[name]


import threading


class FakeRun:
    id = "r-1"

    def __init__(self, status="succeeded", publish=None, failed=None):
        self.status, self.cond = status, threading.Condition()
        self.steps = {"publish": {"name": "publish", "title": "Publish", "status": "succeeded",
                                  "evidence": {"result": publish if publish is not None else {"changed": True, "commit": "abc123"}}}}
        if failed:
            self.steps["discover"] = {"name": "discover", "title": "Discover", "status": "failed", "error": failed}


class FakeRuns:
    def __init__(self, run=None):
        self.started, self.run = [], run or FakeRun()

    def start(self, cfg, steps):
        self.started.append((cfg, steps))
        return self.run


class FakeServerless:
    def __init__(self, saved=(), delivered=None):
        self.store, self.runs, self._delivered = FakeStore(saved), FakeRuns(), delivered or {}

    def delivered(self):
        return self._delivered


INPUTS = {"azure_subscription_id": "s", "workload": "web-b"}


class ChainTests(unittest.TestCase):
    def test_already_onboarded_with_the_same_identity_is_done(self):
        sl = FakeServerless([DONE], {"web-a": DONE})
        r = chain.onboarder(sl)("azure-web-app", "web-a", INPUTS, {"workload_identity_client_id": "c-1", "app_name": "web-a"})
        self.assertEqual(r["state"], "done")
        self.assertEqual(sl.runs.started, [])

    def test_a_new_deployment_gets_a_config_from_its_clouds_settings_and_a_deliver_run(self):
        sl = FakeServerless([DONE], {"web-a": DONE})
        r = chain.onboarder(sl)("azure-web-app@web-b", "web-b", INPUTS,
                                {"workload_identity_client_id": "c-2", "app_name": "web-b", "resource_group": "rg-web-b"})
        self.assertEqual(r, {"state": "published", "commit": "abc123", "link": "/serverless/azure/w/web-b?tab=runs"})
        cfg, steps = sl.runs.started[0]
        self.assertEqual(steps, chain.DELIVERY_STEPS)
        self.assertEqual(cfg["name"], "web-b")
        self.assertEqual(cfg["jfrog"]["url"], "https://acme.jfrog.io")
        self.assertEqual(cfg["target"], {"kind": "azure_web_app", "azure": {"subscription": "s", "resource_group": "rg-web-b", "name": "web-b"}})

    def test_a_changed_identity_is_onboarded_again(self):
        sl = FakeServerless([DONE], {"web-a": DONE})
        r = chain.onboarder(sl)("azure-web-app", "web-a", INPUTS, {"workload_identity_client_id": "c-9", "app_name": "web-a"})
        self.assertEqual(r["state"], "published")

    def test_publish_without_a_change_hands_on_the_onboarding_run_it_queued(self):
        sl = FakeServerless([DONE])
        sl.runs.run = FakeRun(publish={"changed": False, "pipeline": {"run": 77}})
        self.assertEqual(chain.onboarder(sl)("azure-web-app", "web-b", INPUTS, {})["ado_run"], 77)

    def test_a_failed_deliver_run_says_which_step_and_why(self):
        sl = FakeServerless([DONE])
        sl.runs.run = FakeRun("failed", failed="web-b not found")
        r = chain.onboarder(sl)("azure-web-app", "web-b", INPUTS, {})
        self.assertEqual(r["state"], "failed")
        self.assertIn("web-b not found", r["text"])

    def test_a_pull_request_links_to_it(self):
        sl = FakeServerless([DONE])
        sl.runs.run = FakeRun(publish={"changed": True, "pull_request": {"id": 5, "url": "https://pr/5"}})
        r = chain.onboarder(sl)("azure-web-app", "web-b", INPUTS, {})
        self.assertEqual((r["state"], r["link"]), ("started", "https://pr/5"))

    def test_a_run_still_going_after_the_deadline_links_to_it(self):
        sl = FakeServerless([DONE])
        sl.runs.run = FakeRun("running")
        t = iter([0, 0, 10 ** 6])
        r = chain.onboarder(sl, clock=lambda: next(t))("azure-web-app", "web-b", INPUTS, {})
        self.assertEqual(r["state"], "started")

    def test_no_settings_for_the_cloud_sends_the_user_to_the_serverless_page(self):
        r = chain.onboarder(FakeServerless())("azure-web-app", "web-b", INPUTS, {"app_name": "web-b"})
        self.assertEqual(r["state"], "needs_input")


if __name__ == "__main__":
    unittest.main()
