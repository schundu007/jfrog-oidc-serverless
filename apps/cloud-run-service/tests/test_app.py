"""JFrog browser: pages, API, health, errors, headers and logs. No network: JFrog is faked."""

import io
import json
import logging
import os
import unittest
import urllib.error
from unittest import mock

os.environ.setdefault("APP_JSON_LOGS", "0")

from app import create_app, logs, paths  # noqa: E402
from app.artifactory import Artifactory, Denied, NotFound, Unavailable  # noqa: E402
from app.config import Settings  # noqa: E402

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJqZmFjQHgvdXNlcnMveSJ9.c2lnbmF0dXJlLXZhbHVl"

FILES = {"demo-generic/hello.txt": b"hello from jfrog\n"}
TREE = {"demo-generic": [{"name": "hello.txt", "folder": False}, {"name": "docs", "folder": True}],
        "demo-generic/docs": []}


class FakeJFrog:
    """The Artifactory surface the web layer uses."""

    def __init__(self):
        self.fail = None
        self.provider = object()

    def _check(self):
        if self.fail:
            raise self.fail

    def identity(self):
        self._check()
        return {"cloud_identity": "svc@p.iam.gserviceaccount.com", "issuer": "https://accounts.google.com",
                "audience": "jfrog-gcp", "jfrog_user": "gcp-cloudrun/svc-runtime",
                "expires_at": "2026-11-03T05:00:00Z", "expires_in": 3540}

    def ready(self):
        self._check()

    def repositories(self):
        self._check()
        return [{"key": "demo-generic", "type": "local", "package_type": "generic", "description": "demo"}]

    def item(self, path):
        self._check()
        if path in TREE:
            return {"path": path, "folder": True, "children": TREE[path], "created": None, "modified": None}
        if path in FILES:
            return {"path": path, "folder": False, "size": len(FILES[path]), "mime_type": "text/plain",
                    "sha256": "ab" * 32, "created": None, "modified": "2026-11-01T00:00:00Z"}
        raise NotFound(f"{path}: not found")

    def open(self, path):
        return io.BytesIO(FILES[path])


def settings(**kw):
    base = dict(version="abc123", jfrog_url="https://acme.jfrog.io", home_path="demo-generic",
                healthcheck_path="demo-generic/hello.txt", max_download_bytes=1024)
    return Settings(**{**base, **kw})


class AppTests(unittest.TestCase):
    def setUp(self):
        self.jfrog = FakeJFrog()
        self.app = create_app(settings(), artifactory=self.jfrog)
        self.client = self.app.test_client()
        logs._project_read, logs._project = True, "p"   # no metadata server in tests

    def test_home_lists_identity_and_repositories(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        for text in ("svc@p.iam.gserviceaccount.com", "gcp-cloudrun/svc-runtime", "59 min", "/browse/demo-generic", "abc123"):
            self.assertIn(text, body)

    def test_home_still_renders_when_jfrog_is_down(self):
        self.jfrog.fail = Unavailable("JFrog could not be reached")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("JFrog could not be reached", r.get_data(as_text=True))

    def test_browse_folder_and_file(self):
        folder = self.client.get("/browse/demo-generic").get_data(as_text=True)
        self.assertIn("/browse/demo-generic/hello.txt", folder)
        f = self.client.get("/browse/demo-generic/hello.txt")
        self.assertEqual(f.status_code, 200)
        self.assertIn("/download/demo-generic/hello.txt", f.get_data(as_text=True))

    def test_download_streams_the_file_as_an_attachment(self):
        r = self.client.get("/download/demo-generic/hello.txt")
        self.assertEqual((r.status_code, r.data), (200, b"hello from jfrog\n"))
        self.assertEqual(r.headers["Content-Disposition"], 'attachment; filename="hello.txt"')
        self.assertEqual(r.headers["X-Checksum-Sha256"], "ab" * 32)

    def test_download_refuses_folders_and_files_over_the_limit(self):
        self.assertEqual(self.client.get("/download/demo-generic").status_code, 400)
        small = create_app(settings(max_download_bytes=4), artifactory=self.jfrog).test_client()
        self.assertEqual(small.get("/download/demo-generic/hello.txt").status_code, 413)

    def test_bad_paths_are_refused_before_jfrog(self):
        for bad in ("/browse/demo-generic/../secret", "/browse/demo-generic/%2e%2e/x", "/api/v1/items/-bad/x"):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.get(bad).status_code, 400)

    def test_jfrog_failures_map_to_statuses_as_json_on_the_api(self):
        r = self.client.get("/api/v1/items/demo-generic/missing")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "demo-generic/missing: not found")
        self.assertTrue(r.get_json()["request_id"])
        self.jfrog.fail = Denied("demo-generic: this service may not read it")
        self.assertEqual(self.client.get("/api/v1/repositories").status_code, 403)
        self.jfrog.fail = Unavailable("down")
        self.assertEqual(self.client.get("/api/v1/identity").status_code, 502)

    def test_pages_show_errors_as_html(self):
        r = self.client.get("/browse/demo-generic/missing", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("text/html", r.content_type)
        self.assertEqual(self.client.get("/nope").status_code, 404)

    def test_api(self):
        self.assertEqual(self.client.get("/api/v1/identity").get_json()["jfrog_user"], "gcp-cloudrun/svc-runtime")
        self.assertEqual(self.client.get("/api/v1/repositories").get_json()["repositories"][0]["key"], "demo-generic")
        item = self.client.get("/api/v1/items/demo-generic/hello.txt").get_json()
        self.assertEqual((item["folder"], item["size"]), (False, 17))

    def test_liveness_needs_no_jfrog_and_readiness_does(self):
        self.jfrog.fail = Unavailable("down")
        self.assertEqual(self.client.get("/healthz").get_json()["version"], "abc123")
        self.assertEqual(self.client.get("/readyz").status_code, 503)
        self.jfrog.fail = None
        self.assertEqual(self.client.get("/readyz").get_json(), {"status": "ready"})

    def test_healthz_jfrog_is_the_client_selftest(self):
        result = {"ok": True, "claims": {"sub": "1"}, "check": {"username": "u"}, "read": {"status": 200}}
        with mock.patch("app.selftest", return_value=result) as st, mock.patch("app.log_result"):
            r = self.client.get("/healthz/jfrog?fresh=1")
        self.assertEqual((r.status_code, r.get_json()), (200, result))
        st.assert_called_once_with("demo-generic/hello.txt", provider=self.jfrog.provider, fresh=True)
        with mock.patch("app.selftest", return_value={**result, "ok": False}), mock.patch("app.log_result"):
            self.assertEqual(self.client.get("/healthz/jfrog").status_code, 503)

    def test_security_headers_and_request_id(self):
        r = self.client.get("/", headers={"X-Cloud-Trace-Context": "0123456789abcdef0123456789abcdef/1;o=1"})
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Request-Id"], "0123456789abcdef0123456789abcdef")

    def test_static_css_is_served_and_cached(self):
        r = self.client.get("/static/app.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age=3600", r.headers["Cache-Control"])
        r.close()


class ArtifactoryTests(unittest.TestCase):
    def jf(self):
        a = Artifactory(provider_factory=lambda: mock.Mock(jfrog_url="https://acme.jfrog.io"))
        return a

    def http_error(self, code):
        return urllib.error.HTTPError("https://acme.jfrog.io/x", code, "x", {}, None)

    def test_http_errors_become_typed_failures(self):
        for code, cls in ((404, NotFound), (403, Denied), (401, Denied), (500, Unavailable)):
            with self.subTest(code=code), mock.patch("app.artifactory.get_json", side_effect=self.http_error(code)):
                with self.assertRaises(cls):
                    self.jf().item("demo-generic/x")
        with mock.patch("app.artifactory.get_json", side_effect=urllib.error.URLError("dns")):
            with self.assertRaises(Unavailable):
                self.jf().repositories()

    def test_missing_settings_are_unavailable_not_a_crash(self):
        a = Artifactory(provider_factory=lambda: (_ for _ in ()).throw(ValueError("missing JFROG_URL")))
        with self.assertRaises(Unavailable):
            a.ready()

    def test_folder_and_file_shapes(self):
        folder = {"created": "c", "children": [{"uri": "/b.txt", "folder": False}, {"uri": "/a", "folder": True}]}
        with mock.patch("app.artifactory.get_json", return_value=folder):
            self.assertEqual([c["name"] for c in self.jf().item("r/x")["children"]], ["a", "b.txt"])
        f = {"size": "79", "mimeType": "text/plain", "checksums": {"sha256": "s"}, "lastModified": "m"}
        with mock.patch("app.artifactory.get_json", return_value=f):
            self.assertEqual(self.jf().item("r/x.txt"),
                             {"path": "r/x.txt", "created": None, "modified": "m", "folder": False, "size": 79,
                              "mime_type": "text/plain", "sha256": "s"})

    def test_repositories_sorted_and_trimmed(self):
        data = [{"key": "b", "type": "REMOTE", "packageType": "PyPI", "url": "https://x"}, {"key": "a", "type": "LOCAL"}]
        with mock.patch("app.artifactory.get_json", return_value=data):
            self.assertEqual([r["key"] for r in self.jf().repositories()], ["a", "b"])
            self.assertNotIn("url", self.jf().repositories()[1])


class PathTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(paths.clean("/demo-generic/a/b.txt/"), "demo-generic/a/b.txt")
        for bad in ("", "/", "r/../x", "r//x", "r/./x", "r/a\\b", "r/a\x00", ".hidden/x", "x" * 1100):
            with self.subTest(bad=bad), self.assertRaises(paths.BadPath):
                paths.clean(bad)

    def test_filename_and_crumbs(self):
        self.assertEqual(paths.filename('r/we"ird;name.txt'), "we_ird_name.txt")
        self.assertEqual(paths.crumbs("r/a/b"), [("r", "r"), ("a", "r/a"), ("b", "r/a/b")])


class LogTests(unittest.TestCase):
    def record(self, msg, **extra):
        rec = logging.LogRecord("app", logging.INFO, __file__, 1, msg, None, None)
        for k, v in extra.items():
            setattr(rec, k, v)
        return json.loads(logs.JsonFormatter().format(rec))

    def test_json_lines_with_trace_and_no_token(self):
        out = self.record(f"exchanged {TOKEN} and Bearer abcdefghijklmnopqrstuvwxyz",
                          trace_name="projects/p/traces/t", request_id="r1", httpRequest={"status": 200})
        self.assertEqual(out["severity"], "INFO")
        self.assertEqual(out["logging.googleapis.com/trace"], "projects/p/traces/t")
        self.assertNotIn(TOKEN, json.dumps(out))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(out))

    def test_trace_header(self):
        self.assertEqual(logs.trace("0123456789abcdef0123456789abcdef/5;o=1", "p"),
                         ("0123456789abcdef0123456789abcdef", "projects/p/traces/0123456789abcdef0123456789abcdef"))
        self.assertEqual(logs.trace("garbage", "p"), (None, None))

    def test_request_log_never_holds_a_token(self):
        app = create_app(settings(), artifactory=FakeJFrog())
        with self.assertLogs("app", level="INFO") as cm:
            app.test_client().get(f"/browse/demo-generic?x={TOKEN}", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertTrue(cm.records)
        self.assertFalse(any(TOKEN in logs.JsonFormatter().format(r) for r in cm.records))


class SettingsTests(unittest.TestCase):
    def test_home_folder_defaults_to_the_health_check_files(self):
        s = Settings.from_env({"JFROG_HEALTHCHECK_PATH": "demo-generic/hello.txt", "APP_VERSION": "v1"})
        self.assertEqual((s.home_path, s.version, s.max_download_bytes), ("demo-generic", "v1", 100 * 1024 * 1024))
        self.assertEqual(Settings.from_env({"APP_HOME_PATH": "/x/y/"}).home_path, "x/y")
        with self.assertRaises(ValueError):
            Settings.from_env({"APP_MAX_DOWNLOAD_MB": "0"})


if __name__ == "__main__":
    unittest.main()
