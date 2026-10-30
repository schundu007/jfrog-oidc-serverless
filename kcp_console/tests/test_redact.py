"""Known secrets fed through every path that writes or returns text (spec §2):
the shell's log line and recorded fixtures, the ADO bearer, run records, run
events, pipeline logs, the status store, the report and the server log."""

import io
import json
import tempfile
import unittest
from pathlib import Path

from kcp_console import model, redact, report
from kcp_console.ado import Ado
from kcp_console.shell import Shell
from kcp_console.store import RunStore, StatusStore, StoreError

JWT = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEifQ.eyJzdWIiOiJzeXN0ZW06c2VydmljZWFjY291bnQ6amZyb2c6eCJ9.c2lnbmF0dXJl"
REF = "cmVmdGtuOjAxOjE3MDAwMDAwMDA6c2VjcmV0dmFsdWU"
BEARER = "ado-bearer-" + "q" * 30
SECRET_LINES = [
    f"Service Account Token: {JWT}",
    f"--set binaryDownload.auth.accessToken={REF}",
    "Authorization: Bearer abcdefghijklmnop.qrs",
    "JfrogDownloadToken: s3cretvalue2",
    "JFROG_DOWNLOAD_TOKEN=s3cretvalue1",
    '{"artifactory-download-token": "kvsecretvalue4"}',
]
SECRETS = [JWT, REF, "abcdefghijklmnop.qrs", "s3cretvalue2", "s3cretvalue1", "kvsecretvalue4"]
TEXT = "\n".join(SECRET_LINES)


class RedactPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assertClean(self, text, where):
        for s in SECRETS + [BEARER]:
            self.assertNotIn(s, text, f"{where} leaked {s[:12]}…")

    def disk(self):
        return "".join(p.read_text(errors="replace") for p in self.root.rglob("*") if p.is_file())

    def test_shell_log_and_recorded_fixtures(self):
        lines = []
        fx = [{"argv": ["az", "pipelines", "runs", "show", "--id", "1"], "rc": 0, "stdout": TEXT},
              {"http": "GET https://dev.azure.com/o/p/_apis/build/builds/1/logs/1?api-version=7.1", "status": 200, "body": TEXT}]
        sh = Shell(mode="replay", fixtures=fx, log=lines.append)
        sh.run(["az", "pipelines", "runs", "show", "--id", "1"], timeout=5)
        sh.http("GET", "https://dev.azure.com/o/p/_apis/build/builds/1/logs/1?api-version=7.1",
                headers={"Authorization": f"Bearer {BEARER}"})
        self.assertClean("\n".join(lines), "shell log")

    def test_ado_bearer_is_never_logged(self):
        lines = []
        base = "https://dev.azure.com/o/p/_apis/build/builds/7/logs"
        fx = [{"argv": ["az", "account", "get-access-token", "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
                        "--query", "accessToken", "-o", "tsv"], "rc": 0, "stdout": BEARER + "\n"},
              {"http": f"GET {base}?api-version=7.1", "status": 200, "body": json.dumps({"value": [{"id": 1}]})},
              {"http": f"GET {base}/1?api-version=7.1", "status": 200, "body": "pipeline output"}]
        sh = Shell(mode="replay", fixtures=fx, log=lines.append)
        out = Ado(sh, "o", "p").logs(7)
        self.assertEqual(out, "pipeline output")
        self.assertClean("\n".join(lines), "ADO log fetch")

    def test_run_store(self):
        rs = RunStore(self.root)
        run = {"id": "20261029-120000-abcdef", "status": "succeeded", "error": TEXT,
               "request": {"parameters": {"note": SECRET_LINES[0]}}}
        rs.save(run)
        rs.event(run["id"], {"t": "x", "kind": "log", "text": TEXT})
        rs.save_log(run["id"], TEXT)
        self.assertClean(self.disk(), "run store")
        self.assertClean(rs.log(run["id"]), "run log read back")

    def test_status_store_refuses(self):
        st = StatusStore(self.root)
        for line in SECRET_LINES:
            with self.assertRaises(StoreError, msg=line):
                st.update("sandbox-pilot", {"notes": line})
        self.assertClean(self.disk(), "status store")

    def test_report(self):
        cfg = model.default_config()
        diag = {"verdict": {"status": "FAIL", "root_cause": "x", "chain": SECRET_LINES, "proof": None},
                "findings": [{"rule": "r", "score": 1, "title": TEXT, "evidence": TEXT, "label": "", "next": TEXT}], "three_layers": {}}
        rep = report.build(cfg, {"notes": "fine"}, diag, None, [{"id": "r", "error": TEXT}])
        self.assertClean(report.markdown(rep), "report md")
        self.assertClean(report.as_json(rep), "report json")

    def test_server_log(self):
        from kcp_console.server import KcpServer
        from kcp_console.store import Store
        log = io.StringIO()
        srv = KcpServer(port=0, log=log, store=Store(self.root))
        srv.start()
        try:
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            c.request("GET", "/api/session?x=" + REF, headers={"Host": f"127.0.0.1:{srv.port}", "Authorization": f"Bearer {BEARER}"})
            c.getresponse().read()
            c.close()
        finally:
            srv.stop()
        self.assertClean(log.getvalue(), "server log")
        self.assertNotIn(srv.token, log.getvalue())


if __name__ == "__main__":
    unittest.main()
