"""The portable package (pipelines/scripts/export.sh) runs in any Azure, GCP, ADO and JFrog account:
nothing it ships names one. Every target value is entered in the UI (Settings, Infrastructure, JFrog OIDC)."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# Well-known Microsoft IDs, the same in every tenant.
WELL_KNOWN = {
    "499b84ac-1321-427f-aa17-267ca6975798",   # Azure DevOps resource
    "fb60f99c-7a34-4190-8149-302f77469936",   # AzureADTokenExchange
    "04b07795-8ddb-461a-bbee-02f9e1bf7b46",   # Azure CLI client
    # Azure built-in roles: AcrPull, AcrPush, Contributor, Storage Blob Data Owner, Storage Queue Data Contributor,
    # Storage Table Data Contributor, Storage Blob Data Contributor
    "7f951dda-4ed3-4680-a7ca-43fe172d538d", "8311e382-0749-4cb8-b61a-304f252e45ec", "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b", "974c5e8b-45b9-4653-ba55-5f855dd0fb88", "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
}
PLACEHOLDER = re.compile(r"^(?:[0-9a-f]{8}-0000-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|11111111-2222-3333-4444-555555555555"
                         r"|0{8}-0{4}-0{4}-0{4}-[0-9a-f]{12})$", re.I)
TEXT = {".py", ".js", ".sh", ".yml", ".yaml", ".tf", ".json", ".html", ".css", ".ps1", ".java", ".cs", ".example"}


def shipped():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=60).stdout.decode().split("\0")
    files = [f for f in files if f and (ROOT / f).is_file()]
    attrs = subprocess.run(["git", "check-attr", "-z", "export-ignore", "--stdin"], cwd=ROOT, input="\0".join(files).encode(),
                           capture_output=True, timeout=60).stdout.decode().split("\0")
    ignored = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "set"}
    return [f for f in files if f not in ignored]


class PortableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = shipped()

    def test_delivery_names_no_ado_organization_or_project(self):
        ado = json.loads((ROOT / "pipelines/delivery.json").read_text())["ado"]
        self.assertEqual((ado["organization"], ado["project"]), ("", ""))

    def test_no_onboarding_workspace_or_saved_inputs_ship(self):
        self.assertFalse([f for f in self.files if f.startswith(("terraform/workspaces/", "ado-pipelines/", ".console/"))])
        self.assertFalse([f for f in self.files if f.endswith((".tfvars.json", ".tfstate", "terraform.tfvars"))])

    def test_no_account_ids_in_code(self):
        found = []
        for f in self.files:
            if Path(f).suffix not in TEXT or f.startswith("docs/") or "/tests/" in f or "/fixtures/" in f or "/sim/" in f:
                continue
            for g in GUID.findall((ROOT / f).read_text(errors="ignore")):
                if g.lower() not in WELL_KNOWN and not PLACEHOLDER.match(g):
                    found.append(f"{f}: {g}")
        self.assertEqual(found, [])

    def test_pipelines_take_the_agent_pool_from_vars(self):
        for f in self.files:
            if f.startswith("pipelines/") and f.endswith(".yml") and "vmImage" in (ROOT / f).read_text():
                self.assertIn("variables.AGENT_POOL", (ROOT / f).read_text(), f)

    def test_every_azure_pipeline_is_registered_from_ado(self):
        # Every Azure pipeline runs from ADO: the one pipeline created in the ADO UI (Register pipelines)
        # registers all the others.
        script = (ROOT / "pipelines/scripts/register-azure-pipelines.sh").read_text()
        listed = {f for f in re.findall(r'"(pipelines/[^|"]+\.yml)\|', script) if "$" not in f}
        listed |= {f"pipelines/serverless/azure/{s}/{y}.yml" for s in re.findall(r'"(azure-[a-z-]+):', script)
                   for y in ("stack-setup", "build-deploy")}
        azure = {f for f in self.files if f.startswith("pipelines/") and f.endswith(".yml") and "/templates/" not in f
                 and "/vars/" not in f and ("/azure/" in f or Path(f).name.startswith("azure-"))}
        self.assertEqual(azure - listed, set())
        self.assertFalse(listed - azure)
        # No bootstrap (Owner) connection: the admin applies the pipeline identity with their own sign-in.
        self.assertFalse((ROOT / "pipelines/serverless/azure/azure-pipeline-identity/stack-setup.yml").exists())
        for f in self.files:
            if f.startswith("pipelines/"):
                self.assertNotIn("bootstrap", (ROOT / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()
