"""Container Apps jobs: discover reads `az containerapp job show`, wire updates the job."""

import json
import unittest

from console import model
from console.shell import Result, Shell
from console.steps import discover
from console.steps.wire import Wire

SUB, RG, NAME = "00000000-0000-0000-0000-000000000001", "rg-x", "caj-x"
PID = "11111111-2222-3333-4444-555555555555"
JOB = {
    "id": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.App/jobs/{NAME}",
    "location": "westus2",
    "identity": {"type": "UserAssigned", "userAssignedIdentities": {
        f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-x":
            {"principalId": PID, "clientId": "c1"}}},
    "properties": {"environmentId": f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.App/managedEnvironments/cae-x",
                   "template": {"containers": [{"name": "app", "image": "crx.azurecr.io/app:1", "env": []}]},
                   "configuration": {"registries": [], "secrets": []}},
}


class Fake(Shell):
    def __init__(self):
        super().__init__(mode="replay")
        self.calls = []

    def _replay_cmd(self, argv):
        self.calls.append(list(argv))
        if argv[:4] == ["az", "containerapp", "job", "show"]:
            return Result(argv, 0, json.dumps(JOB), "", 0.0)
        if argv[:2] == ["az", "identity"]:
            return Result(argv, 0, json.dumps({"principalId": PID, "clientId": "c1", "tenantId": "t1"}), "", 0.0)
        self.misses.append(" ".join(argv))
        return Result(argv, 127, "", "no fixture", 0.0)


def cfg():
    c = model.default_config("azure_container_app_job", name=NAME)
    c["target"]["azure"].update(subscription=SUB, resource_group=RG, name=NAME)
    c["jfrog"]["url"] = "https://acme.jfrog.io"
    c["pull_path"]["enabled"] = False
    return c


class JobTests(unittest.TestCase):
    def test_kind_exists(self):
        self.assertEqual(model.cloud_of("azure_container_app_job"), "azure")

    def test_discover_uses_job_show(self):
        sh = Fake()
        res = discover.run(cfg(), sh)
        self.assertTrue(any(c[:4] == ["az", "containerapp", "job", "show"] for c in sh.calls))
        self.assertFalse(any(c[:3] == ["az", "containerapp", "show"] for c in sh.calls))
        self.assertNotIn("fail", res["status"], res)
        self.assertTrue(res["config_patch"]["target.azure.environment_id"].endswith("/cae-x"))

    def test_wire_updates_the_job(self):
        c = cfg()
        c["target"]["discovered"] = {"kind": "azure_container_app_job", "resource_id": JOB["id"],
                                     "mapping_subject": PID, "principal_id": PID, "client_id": "c1"}
        plan = Wire(c, Fake(), {"azure_audience_app_id": "app-1", "jfrog_provider_name": "azure-entra",
                                "audience_uri": "api://app-1"}).plan()
        self.assertTrue(plan["commands"])
        self.assertTrue(all(cmd[:3] == ["az", "containerapp", "job"] for cmd in plan["commands"]), plan["commands"])


if __name__ == "__main__":
    unittest.main()
