"""shell.py: argv-only execution, timeouts, env allow-list, redaction, replay/record."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from console import shell

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
PY = sys.executable


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.sh = shell.Shell(log=self.lines.append)

    def test_runs_argv_and_captures(self):
        res = self.sh.run([PY, "-c", "import sys; print('hi'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual(res.rc, 3)
        self.assertEqual(res.stdout.strip(), "hi")
        self.assertEqual(res.stderr.strip(), "err")

    def test_no_shell_interpretation(self):
        res = self.sh.run([PY, "-c", "import sys; print(sys.argv[1])", "$(echo pwned); rm -rf /"])
        self.assertEqual(res.stdout.strip(), "$(echo pwned); rm -rf /")

    def test_rejects_string_command(self):
        with self.assertRaises(TypeError):
            self.sh.run("ls -l")
        with self.assertRaises(TypeError):
            self.sh.run([])

    def test_timeout(self):
        res = self.sh.run([PY, "-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertEqual(res.rc, 124)
        self.assertIn("timed out", res.stderr)

    def test_missing_binary(self):
        self.assertEqual(self.sh.run(["definitely-not-a-binary-xyz"]).rc, 127)

    def test_stdin_closed(self):
        res = self.sh.run([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=5)
        self.assertEqual(res.stdout.strip(), "''")

    def test_env_allow_list(self):
        with mock.patch.dict(os.environ, {"JFROG_ACCESS_TOKEN": "secret-value-123", "AWS_SECRET_ACCESS_KEY": "x",
                                          "AZURE_CONFIG_DIR": "/tmp/az", "LC_ALL": "C"}):
            res = self.sh.run([PY, "-c", "import os, json; print(json.dumps(dict(os.environ)))"])
            env = json.loads(res.stdout)
            self.assertNotIn("JFROG_ACCESS_TOKEN", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertEqual(env["AZURE_CONFIG_DIR"], "/tmp/az")
            self.assertEqual(env["LC_ALL"], "C")
            res = self.sh.run([PY, "-c", "import os; print(os.environ['JFROG_ACCESS_TOKEN'] == 'given')"],
                              extra_env={"JFROG_ACCESS_TOKEN": "given"})
            self.assertEqual(res.stdout.strip(), "True")

    def test_log_line_is_redacted(self):
        self.sh.run([PY, "-c", "pass", "--password", "hunter2hunter2", JWT])
        text = "\n".join(self.lines)
        self.assertIn("exit 0", text)
        self.assertNotIn("hunter2hunter2", text)
        self.assertNotIn(JWT, text)

    def test_https_only(self):
        with self.assertRaises(ValueError):
            self.sh.http("GET", "http://example.com/")


class ReplayRecordTests(unittest.TestCase):
    def test_replay_commands_and_http(self):
        sh = shell.Shell(mode="replay", fixtures=[
            {"argv": ["gcloud", "version", "--format=json"], "rc": 0, "stdout": '{"Google Cloud SDK": "500.0.0"}'},
            {"http": "GET https://acme.jfrog.io/access/api/v1/system/ping", "status": 200, "body": "OK"},
        ])
        res = sh.run(["/opt/homebrew/bin/gcloud", "version", "--format=json"])
        self.assertTrue(res.ok)
        self.assertEqual(res.json()["Google Cloud SDK"], "500.0.0")
        self.assertEqual(sh.http("GET", "https://acme.jfrog.io/access/api/v1/system/ping").body, "OK")
        self.assertEqual(sh.run(["az", "account", "show"]).rc, 127)
        self.assertEqual(sh.http("GET", "https://acme.jfrog.io/x").status, 0)
        self.assertEqual(sh.misses, ["az account show", "GET https://acme.jfrog.io/x"])

    def test_record_redacts_and_hides_sensitive(self):
        sh = shell.Shell(mode="record")
        sh.run([PY, "-c", f"print('{JWT}')"])
        sh.run([PY, "-c", "print('ya29.token-like-value')"], sensitive=True)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fx.json"
            sh.save_fixtures(path)
            text = path.read_text()
            data = shell.load_fixtures(path)
        self.assertNotIn(JWT, text)
        self.assertNotIn("ya29.token-like-value", text)
        self.assertEqual(data[1]["stdout"], shell.REDACTED_OUTPUT)
        self.assertEqual(Path(data[0]["argv"][0]).name, Path(PY).name)

    def test_load_fixture_dir(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.json").write_text('[{"argv": ["a"], "rc": 0}]')
            Path(d, "b.json").write_text('[{"argv": ["b"], "rc": 1}]')
            self.assertEqual(len(shell.load_fixtures(d)), 2)


if __name__ == "__main__":
    unittest.main()


class ConsumeTests(unittest.TestCase):
    def test_consumed_fixture_answers_once(self):
        sh = shell.Shell(mode="replay", fixtures=[{"argv": ["az", "x"], "stdout": "before", "consume": True},
                                            {"argv": ["az", "x"], "stdout": "after"}])
        self.assertEqual(sh.run(["az", "x"]).stdout, "before")
        self.assertEqual(sh.run(["az", "x"]).stdout, "after")
        self.assertEqual(sh.run(["az", "x"]).stdout, "after")
