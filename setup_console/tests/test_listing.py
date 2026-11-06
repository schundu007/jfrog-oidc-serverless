"""The Infrastructure and JFrog OIDC pages' listing rule (static/listing.js), run under node: every
stack counted in Essentials is a row on the live page, and residue is listed, never hidden."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

LISTING = Path(__file__).resolve().parents[1] / "static" / "listing.js"
NODE = shutil.which("node")

PARTLY = {"id": "azure-pipeline-identity", "status": {"deployed": False, "deleted": "Partly deleted outside the console.",
                                               "outputs": {"azure_tfstate": {"storage_account": "st"}}}}
DEPLOYED = {"id": "gcp-pipeline-identity", "status": {"deployed": True, "outputs": {"x": 1}}}
FAILED = {"id": "azure-functions-flex", "status": {}, "failed": {"action": "apply"}}
NEVER = {"id": "gcp-cloud-run", "status": {}}
GONE = {"id": "gcp-cloud-run", "status": {"deployed": False, "deleted": "Deleted.", "outputs": {}}}
BUSY = {"id": "azure-container-app", "status": {}, "busy": True}


def run(stacks):
    js = (f"import * as L from {json.dumps(LISTING.as_uri())};"
          f"const s = {json.dumps(stacks)};"
          "console.log(JSON.stringify({counts: L.counts(s), listed: s.filter(L.standing).map((x) => x.id)}));")
    out = subprocess.run([NODE, "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class ListingTests(unittest.TestCase):
    def test_counts_match_the_rows(self):
        r = run([PARTLY, DEPLOYED, FAILED, NEVER, GONE, BUSY])
        self.assertEqual(sorted(r["listed"]), ["azure-container-app", "azure-functions-flex", "azure-pipeline-identity", "gcp-pipeline-identity"])
        self.assertEqual(r["counts"], {"listed": 4, "deployed": 1, "attention": 2, "busy": 1, "archived": 2})

    def test_residue_is_listed(self):
        """The screen that said "Needs attention 1" over "Nothing deployed": a partly deleted pipeline identity."""
        r = run([PARTLY, NEVER])
        self.assertEqual(r["listed"], ["azure-pipeline-identity"])
        self.assertEqual(r["counts"]["attention"], r["counts"]["listed"])

    def test_nothing_standing(self):
        r = run([NEVER, GONE])
        self.assertEqual(r["listed"], [])
        self.assertEqual(r["counts"], {"listed": 0, "deployed": 0, "attention": 0, "busy": 0, "archived": 2})


if __name__ == "__main__":
    unittest.main()
