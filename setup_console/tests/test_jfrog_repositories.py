"""Each cloud's JFrog vars file names exactly the repositories its trust stack creates."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class AzureRepositoriesTests(unittest.TestCase):
    def setUp(self):
        tf = (ROOT / "terraform/stacks/oidc/azure/azure-jfrog-trust/repositories.tf").read_text()
        block = re.search(r"remotes\s*=\s*\{(.*?)\}", tf, re.S).group(1)
        self.remotes = dict(re.findall(r'(\w+)\s*=\s*"([^"]+)"', block))
        self.vars = dict(re.findall(r"^\s+(JFROG_\w+):\s*(\S+)\s*$",
                                    (ROOT / "pipelines/vars/azure-test-jfrog.yml").read_text(), re.M))

    def test_vars_file_names_the_trust_stacks_remotes(self):
        self.assertEqual(self.vars, {
            "JFROG_DOCKER_REMOTE": self.remotes["docker"],
            "JFROG_PYPI_REPO": self.remotes["pypi"],
            "JFROG_MAVEN_REPO": self.remotes["maven"],
            "JFROG_NUGET_REPO": self.remotes["nuget"],
        "JFROG_NPM_REPO": self.remotes["npm"],
        "JFROG_COMPOSER_REPO": self.remotes["composer"],
        })

    def test_every_key_is_this_clouds_own(self):
        for key in self.remotes.values():
            self.assertTrue(key.startswith("azure-"), key)


if __name__ == "__main__":
    unittest.main()
