"""The only way the console runs a command or calls a cloud/JFrog API (spec §6.1).

- argv lists only, never a shell; the executable is resolved with shutil.which.
- Every call has a timeout. stdin is closed, so nothing can prompt.
- The child environment is an allow-list: PATH, HOME, locale, proxy/CA settings,
  and the CLIs' own config variables. JFROG_ACCESS_TOKEN and other secrets are
  only passed when a caller hands them over explicitly (extra_env).
- A log line per call goes through redact; sensitive calls (ones that print a
  token, e.g. `gcloud auth print-access-token`) never have their output logged
  or recorded.
- HTTP calls never follow redirects, so an Authorization header cannot be
  forwarded to another host.

Modes:
- live: run for real.
- replay: answer from recorded fixtures (tests, demos, CI). Nothing is executed
  and nothing touches the network. Unmatched calls fail with exit 127 and are
  listed in .misses.
- record: run for real and keep a redacted copy of every call, to be saved with
  save_fixtures() and replayed later.

Fixture file: JSON list of
  {"argv": [...], "rc": 0, "stdout": "...", "stderr": ""}
  {"http": "GET https://...", "status": 200, "body": "..."}
argv[0] is matched by basename; the first match answers. A fixture with
"consume": true answers once and is then dropped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import redact, words

ENV_ALLOW = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TEMP", "TMP", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
    "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "CLOUDSDK_CORE_PROJECT", "CLOUDSDK_PYTHON",
    "AZURE_CONFIG_DIR", "AZURE_EXTENSION_DIR", "AZURE_CORE_ONLY_SHOW_ERRORS",
    "TF_CLI_CONFIG_FILE", "TF_PLUGIN_CACHE_DIR", "SYSTEMROOT", "COMSPEC", "PATHEXT",
    # AWS: profiles and files, and web identity (a pipeline's OIDC token file + role). Raw keys
    # (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN) are not passed: use a profile.
    "AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_REGION", "AWS_DEFAULT_REGION",
    "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_SESSION_NAME", "AWS_CA_BUNDLE", "AWS_PAGER",
    # Kubernetes / OpenShift
    "KUBECONFIG", "KUBE_CONFIG_PATH",
}
ENV_ALLOW_PREFIXES = ("LC_",)
REDACTED_OUTPUT = "[REDACTED: sensitive output not recorded]"


@dataclass
class Result:
    argv: list
    rc: int
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def json(self, default=None):
        try:
            return json.loads(self.stdout)
        except ValueError:
            return default

    def error_text(self, limit: int = 400) -> str:
        text = (self.stderr or self.stdout or "").strip()
        return redact.text(text[-limit:])


@dataclass
class HttpResult:
    method: str
    url: str
    status: int            # 0 when no HTTP response arrived
    body: str = ""
    error: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self, default=None):
        try:
            return json.loads(self.body)
        except ValueError:
            return default


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface the 3xx instead of re-sending credentials elsewhere


_OPENER = urllib.request.build_opener(_NoRedirect)


def child_env(extra_env: dict | None = None, base: dict | None = None) -> dict:
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}
    env.update(extra_env or {})
    return env


@dataclass
class Shell:
    mode: str = "live"                 # live | replay | record
    fixtures: list = field(default_factory=list)
    log: object = None                 # callable(str) for one redacted line per call
    which: object = shutil.which
    misses: list = field(default_factory=list)
    recorded: list = field(default_factory=list)

    @classmethod
    def replay(cls, path, **kw) -> "Shell":
        return cls(mode="replay", fixtures=load_fixtures(path), **kw)

    # -- commands ----------------------------------------------------------------

    def run(self, argv: list, timeout: float = 60.0, sensitive: bool = False, extra_env: dict | None = None,
            shown: list | None = None, stream: bool = False) -> Result:
        """shown: what to log and record instead of argv, for an argv that carries values nobody may see
        (e.g. a whole Lambda environment sent back with one managed change).
        stream: log each stdout line (redacted) as the command prints it, for long commands whose
        progress the operator watches (terraform plan and apply). Never for sensitive output."""
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
            raise TypeError("argv must be a non-empty list of strings")
        argv = list(argv)
        logged = list(shown) if shown else argv
        if self.mode == "replay":
            res = self._replay_cmd(argv)
        else:
            res = self._exec(argv, timeout, extra_env, stream=stream and not sensitive)
            if self.mode == "record":
                self.recorded.append({"argv": [Path(logged[0]).name, *redact.argv(logged[1:])], "rc": res.rc,
                                      "stdout": REDACTED_OUTPUT if sensitive else redact.text(res.stdout),
                                      "stderr": redact.text(res.stderr)})
        self._log(redact.text(words.command(redact.argv(logged), res.rc, res.seconds)))
        return res

    def _exec(self, argv, timeout, extra_env, stream=False) -> Result:
        exe = self.which(argv[0])
        started = time.monotonic()
        if not exe:
            return Result(argv, 127, "", f"{argv[0]}: command not found on PATH", 0.0)
        if stream:
            return self._exec_stream(argv, [exe, *argv[1:]], timeout, extra_env, started)
        try:
            proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                                  env=child_env(extra_env), stdin=subprocess.DEVNULL, check=False)
            return Result(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started)
        except subprocess.TimeoutExpired:
            return Result(argv, 124, "", f"{argv[0]} timed out after {timeout:.0f}s", time.monotonic() - started)
        except OSError as exc:
            return Result(argv, 126, "", f"{argv[0]}: {exc}", time.monotonic() - started)

    def _exec_stream(self, argv, cmd, timeout, extra_env, started) -> Result:
        """As _exec, but stdout is read line by line and logged as it comes; stderr is drained by a
        thread so neither pipe fills up. The timeout kills the process, as subprocess.run would."""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    text=True, env=child_env(extra_env))
        except OSError as exc:
            return Result(argv, 126, "", f"{argv[0]}: {exc}", time.monotonic() - started)
        err, timed_out = [], threading.Event()
        drain = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
        drain.start()

        def kill():
            timed_out.set()
            proc.kill()
        timer = threading.Timer(timeout, kill)
        timer.start()
        out = []
        try:
            for line in proc.stdout:
                out.append(line)
                if line.strip():
                    self._log(line.rstrip("\n"))
            proc.wait()
        finally:
            timer.cancel()
            drain.join(5)
        if timed_out.is_set():
            return Result(argv, 124, "".join(out), f"{argv[0]} timed out after {timeout:.0f}s", time.monotonic() - started)
        return Result(argv, proc.returncode, "".join(out), "".join(err), time.monotonic() - started)

    def _replay_cmd(self, argv) -> Result:
        want = [Path(argv[0]).name, *argv[1:]]
        for i, fx in enumerate(self.fixtures):
            if fx.get("argv") == want:
                if fx.get("consume"):          # answer once: lets a test show state before and after a change
                    del self.fixtures[i]
                return Result(argv, int(fx.get("rc", 0)), fx.get("stdout", ""), fx.get("stderr", ""), 0.0)
        self.misses.append(" ".join(want))
        return Result(argv, 127, "", "no recorded fixture for this command", 0.0)

    # -- HTTP ----------------------------------------------------------------------

    def http(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
             timeout: float = 20.0, sensitive: bool = False) -> HttpResult:
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("HTTP calls must use https")
        if self.mode == "replay":
            res = self._replay_http(method, url)
        else:
            res = self._fetch(method, url, headers or {}, body, timeout)
            if self.mode == "record":
                self.recorded.append({"http": f"{method} {url}", "status": res.status,
                                      "body": REDACTED_OUTPUT if sensitive else redact.text(res.body)})
        self._log(redact.text(words.http(method, url, res.status or res.error, res.seconds)))
        return res

    def _fetch(self, method, url, headers, body, timeout) -> HttpResult:
        started = time.monotonic()
        req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json", **headers})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                text = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
                return HttpResult(method, url, resp.status, text, "", time.monotonic() - started)
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read(64 * 1024).decode("utf-8", "replace")
            finally:
                exc.close()
            return HttpResult(method, url, exc.code, text, "", time.monotonic() - started)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            return HttpResult(method, url, 0, "", redact.text(str(reason)), time.monotonic() - started)

    def _replay_http(self, method, url) -> HttpResult:
        key = f"{method} {url}"
        for fx in self.fixtures:
            if fx.get("http") == key:
                return HttpResult(method, url, int(fx.get("status", 200)), fx.get("body", ""))
        self.misses.append(key)
        return HttpResult(method, url, 0, "", "no recorded fixture for this request")

    # -- misc ------------------------------------------------------------------------

    def _log(self, line: str) -> None:
        if self.log:
            self.log(redact.text(line))

    def save_fixtures(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.recorded, indent=2) + "\n")


def load_fixtures(path) -> list:
    """A fixture file, or a directory whose *.json files (recursively) are concatenated."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no fixtures at {path}")
    files = sorted(path.rglob("*.json")) if path.is_dir() else [path]
    out = []
    for f in files:
        data = json.loads(f.read_text())
        if not isinstance(data, list):
            raise ValueError(f"{f}: fixture file must be a JSON list")
        out.extend(data)
    return out
