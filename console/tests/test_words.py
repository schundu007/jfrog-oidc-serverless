"""Plain-language lines for finished commands (console/words.py)."""

import unittest

from console import words


class CommandWordsTests(unittest.TestCase):
    def test_plan_with_changes_is_not_a_failure(self):
        argv = ["terraform", "-chdir=/w", "plan", "-out=tfplan", "-detailed-exitcode"]
        self.assertEqual(words.command(argv, 2, 9.1), "Planned the Terraform changes: ok, changes to make, 9.1s")
        self.assertEqual(words.command(argv, 0, 1.0), "Planned the Terraform changes: ok, no changes, 1.0s")
        self.assertEqual(words.command(argv, 1, 1.0), "Planned the Terraform changes: failed (exit 1), 1.0s")

    def test_other_exit_codes_unchanged(self):
        self.assertEqual(words.command(["terraform", "apply"], 2, 1.0), "Applied the Terraform plan: failed (exit 2), 1.0s")


if __name__ == "__main__":
    unittest.main()
