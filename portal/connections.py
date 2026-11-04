"""Settings: who the consoles act as, entered on the page instead of the machine's own logins.

One connection per tool. Each starts on this machine's own login (~/.config/gcloud, ~/.azure,
an exported JFROG_ACCESS_TOKEN); signing in on Settings switches it to the console's own:

- Google Cloud: `gcloud auth login --no-launch-browser --update-adc` into .console/connections/gcloud
  (CLOUDSDK_CONFIG, and GOOGLE_APPLICATION_CREDENTIALS for Terraform). The page shows Google's link;
  the operator pastes the code back.
- Azure: `az login --use-device-code` into .console/connections/azure (AZURE_CONFIG_DIR). The page shows
  the device code. Azure DevOps signs in through it.
- Azure DevOps: organization, project, repository and branch (no secret: the Azure sign-in's token).
- JFrog: platform URL and an admin access token. The token lives in this process only
  (JFROG_ACCESS_TOKEN, which every redactor masks); it is never written, logged or returned.

What gcloud and az cache in their config directories is their own storage, in a 0700 directory
outside git. settings.json holds only non-secret choices. Every process env change goes through
apply(), so the consoles' shells (which pass CLOUDSDK_CONFIG, AZURE_CONFIG_DIR ... through
their allow-list) pick it up on their next call.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from console import redact
from console.shell import Shell

TOOLS = ("gcp", "azure", "ado", "jfrog")
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
ADO_KEYS = ("organization", "project", "repository", "target_branch")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
GUID_OR_DOMAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,253}$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/?$")
DEVICE_URL = re.compile(r"https://\S*(?:devicelogin|device|activate)\S*", re.I)
DEVICE_CODE = re.compile(r"\b(?:code|enter)\b[^A-Z0-9]*\b([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})?)\b")
GOOGLE_URL = re.compile(r"https://accounts\.google\.com/\S+")
PENDING_SECONDS = 900


class SettingsError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Connections:
    def __init__(self, root: Path, shell_factory=None, environ=None, on_ado=None, clock=time.time):
        self.root = Path(root) / "connections"
        self.file = self.root / "settings.json"
        self.shell_factory = shell_factory or (lambda: Shell(mode="live", log=lambda line: None))
        self.environ = os.environ if environ is None else environ
        self.on_ado = on_ado            # callable(ado dict): retarget the running Azure DevOps client
        self.clock = clock
        self._lock = threading.Lock()
        self._signin = {}               # tool -> {"session", "started", "error"}
        self._jfrog_token = ""          # in memory only
        self._jfrog_login = "machine"   # which token JFrog calls use: the entered one or the machine's
        self.jfrog_session_id = lambda: str(uuid.uuid4())   # browser sign-in session (seam for tests)
        self.jfrog_poll = 3.0
        self._machine = {k: self.environ.get(k) for k in ("CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS",
                                                         "AZURE_CONFIG_DIR",
                                                         "JFROG_ACCESS_TOKEN")}

    # -- stored choices (non-secret) -----------------------------------------------------------

    def settings(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if redact.text(text) != text:
            raise SettingsError("refusing to save a value that looks like a secret")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def _update(self, tool: str, values: dict | None) -> dict:
        with self._lock:
            data = self.settings()
            if values is None:
                data.pop(tool, None)
            else:
                data[tool] = values
            self._save(data)
            return data

    def dir_of(self, tool: str) -> Path:
        return self.root / {"gcp": "gcloud", "azure": "azure"}[tool]

    # -- process environment -------------------------------------------------------------------

    def apply(self) -> None:
        """Point the process at each connection's login: the console's own where signed in here,
        else whatever the machine had when the portal started."""
        s = self.settings()
        env = self.environ

        def put(k, v):
            if v:
                env[k] = v
            else:
                env.pop(k, None)
        g = self.dir_of("gcp")
        if (s.get("gcp") or {}).get("mode") == "console" and g.is_dir():
            put("CLOUDSDK_CONFIG", str(g))
            adc = g / "application_default_credentials.json"
            put("GOOGLE_APPLICATION_CREDENTIALS", str(adc) if adc.is_file() else self._machine["GOOGLE_APPLICATION_CREDENTIALS"])
        else:
            put("CLOUDSDK_CONFIG", self._machine["CLOUDSDK_CONFIG"])
            put("GOOGLE_APPLICATION_CREDENTIALS", self._machine["GOOGLE_APPLICATION_CREDENTIALS"])
        a = self.dir_of("azure")
        put("AZURE_CONFIG_DIR", str(a) if (s.get("azure") or {}).get("mode") == "console" and a.is_dir()
            else self._machine["AZURE_CONFIG_DIR"])
        put("JFROG_ACCESS_TOKEN", self._jfrog_token if self._jfrog_login == "console" and self._jfrog_token
            else self._machine["JFROG_ACCESS_TOKEN"])

    # A login exists where its CLI keeps credentials: gcloud's credentials.db, az's MSAL token cache.
    _CRED_FILES = {"gcp": ("credentials.db",), "azure": ("msal_token_cache.json", "msal_token_cache.bin")}

    def _machine_dir(self, tool: str) -> Path:
        if tool == "gcp":
            return Path(self._machine.get("CLOUDSDK_CONFIG") or Path.home() / ".config" / "gcloud")
        return Path(self._machine.get("AZURE_CONFIG_DIR") or Path.home() / ".azure")

    def logins(self, tool: str) -> dict:
        """Which logins exist to switch between: this machine's and the one signed in on this console."""
        if tool == "jfrog":
            return {"machine": bool(self._machine.get("JFROG_ACCESS_TOKEN")), "console": bool(self._jfrog_token)}
        has = lambda d: any((d / f).is_file() for f in self._CRED_FILES[tool])   # noqa: E731
        return {"machine": has(self._machine_dir(tool)), "console": has(self.dir_of(tool))}

    def use(self, tool: str, body: dict) -> dict:
        """Switch which login a tool acts as. Neither login is removed, so switching back is instant."""
        login = str(body.get("login") or "")
        if tool not in ("gcp", "azure", "jfrog"):
            raise SettingsError(f"{tool} has one login only", 404)
        if login not in ("machine", "console"):
            raise SettingsError("login is machine or console")
        if login == "console" and not self.logins(tool)["console"]:
            raise SettingsError("sign in on this console first", 409)
        if tool == "jfrog":
            self._jfrog_login = login
        else:
            keep = {k: v for k, v in (self.settings().get(tool) or {}).items() if k != "mode"}
            self._update(tool, {**keep, "mode": "console"} if login == "console" else (keep or None))
        self.apply()
        return {"login": login}

    def _env_for(self, tool: str) -> dict:
        """The child env for a sign-in into the console's own directory."""
        d = self.dir_of(tool)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(d, 0o700)
        return {"gcp": {"CLOUDSDK_CONFIG": str(d)}, "azure": {"AZURE_CONFIG_DIR": str(d)}}[tool]

    # -- status -------------------------------------------------------------------------------

    def status(self) -> dict:
        # Each tool on its own shell, all at once: the page waits for the slowest CLI, not their sum.
        s = self.settings()
        readers = {"gcp": self._gcp, "azure": self._azure, "ado": self._ado, "jfrog": self._jfrog}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(readers), thread_name_prefix="settings") as pool:
            futures = {k: pool.submit(fn, self.shell_factory(), s) for k, fn in readers.items()}
            return {k: f.result() for k, f in futures.items()}

    def _source(self, tool: str, s: dict) -> str:
        return "console" if (s.get(tool) or {}).get("mode") == "console" else "machine"

    def _pending(self, tool: str) -> dict | None:
        p = self._signin.get(tool)
        if not p:
            return None
        if tool == "jfrog":
            sess = p["session"]
            return {"state": ("failed" if sess.rc else "done") if sess.done else "waiting", "url": sess.url,
                    "code": sess.code, "lines": [], "needs_code": False, **({"error": sess.error} if sess.rc else {})}
        sess, lines = p["session"], p["session"].lines
        text = "\n".join(lines)
        out = {"state": "done" if sess.done else "waiting", "lines": lines[-12:]}
        url = ((GOOGLE_URL.search(text) if tool == "gcp" else None) or DEVICE_URL.search(text))
        if url:
            out["url"] = url.group(0).rstrip(".,)")
        code = DEVICE_CODE.search(text) if tool != "gcp" else None
        if code:
            out["code"] = code.group(1)
        out["needs_code"] = tool == "gcp" or "authorization code" in text.lower()
        if sess.done:
            out["state"] = "failed" if sess.rc else "done"
            if sess.rc:
                out["error"] = p.get("error") or (lines[-1] if lines else f"exit {sess.rc}")
        return out

    def _gcp(self, sh, s) -> dict:
        r = sh.run(["gcloud", "config", "list", "--format=json"], timeout=30)
        core = ((r.json({}) or {}).get("core") or {}) if r.ok else {}
        out = {"source": self._source("gcp", s), "account": core.get("account") or "",
               "project": core.get("project") or "", "cli": r.rc != 127, "logins": self.logins("gcp")}
        out["signed_in"] = bool(out["account"])
        out["pending"] = self._pending("gcp")
        return out

    def _azure(self, sh, s) -> dict:
        r = sh.run(["az", "account", "show", "-o", "json"], timeout=30)
        acc = r.json({}) if r.ok else {}
        out = {"source": self._source("azure", s), "account": ((acc or {}).get("user") or {}).get("name") or "",
               "subscription": (acc or {}).get("name") or "", "subscription_id": (acc or {}).get("id") or "",
               "tenant": (acc or {}).get("tenantId") or "", "tenant_wanted": (s.get("azure") or {}).get("tenant") or "",
               "cli": r.rc != 127, "logins": self.logins("azure")}
        out["signed_in"] = bool(out["account"])
        out["pending"] = self._pending("azure")
        return out

    def _ado(self, sh, s) -> dict:
        from console import deliver
        repo = Path(__file__).resolve().parent.parent
        base = {}
        try:
            base = (json.loads((repo / "pipelines" / "delivery.json").read_text()).get("ado") or {})
        except (OSError, ValueError):
            pass
        vals = {**{k: base.get(k) or "" for k in ADO_KEYS}, **deliver.ado_overrides(self.file)}
        out = {"values": vals, "account": "", "signed_in": False, "project_ok": None}
        r = sh.run(["az", "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken",
                    "-o", "tsv"], timeout=30, sensitive=True)
        tok = r.stdout.strip() if r.ok else ""
        if not tok:
            out["error"] = "Sign in to Azure first: Azure DevOps uses that sign-in."
            return out
        hdr = {"Authorization": f"Bearer {tok}"}
        me = sh.http("GET", "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1", headers=hdr)
        if me.ok:
            p = me.json({}) or {}
            out["account"], out["signed_in"] = p.get("emailAddress") or p.get("displayName") or "", True
        if vals["organization"] and vals["project"]:
            pr = sh.http("GET", f"https://dev.azure.com/{vals['organization']}/_apis/projects/"
                         f"{vals['project']}?api-version=7.1", headers=hdr)
            out["project_ok"] = pr.ok
            if not pr.ok:
                out["error"] = (f"Project {vals['project']} in {vals['organization']} answered "
                                f"{pr.status or pr.error}: check the names and that this account is a member.")
        return out

    def _jfrog(self, sh, s) -> dict:
        from console import jfrog_operator
        url = (s.get("jfrog") or {}).get("url") or ""
        tok = self.environ.get("JFROG_ACCESS_TOKEN") or ""
        ident = jfrog_operator.identity()
        console = self._jfrog_login == "console" and bool(self._jfrog_token)
        out = {"url": url, "source": "console" if console else ("machine" if tok else ""), "logins": self.logins("jfrog"),
               "token_present": bool(tok), "operator": ident.get("JFROG_OIDC_SERVICE_ACCOUNT") or "",
               "signed_in": False, "admin": None, "pending": self._pending("jfrog")}
        if tok and url:
            out.update(self._check_jfrog(sh, url, tok))
        return out

    @staticmethod
    def _check_jfrog(sh, url: str, tok: str) -> dict:
        r = sh.http("GET", url.rstrip("/") + "/access/api/v1/oidc", headers={"Authorization": f"Bearer {tok}"},
                    sensitive=True)
        if r.status == 200:
            return {"signed_in": True, "admin": True}
        if r.status == 403:
            return {"signed_in": True, "admin": False, "error": "The token works but is not an admin token."}
        if r.status == 401:
            return {"signed_in": False, "error": "JFrog rejected the token: expired, revoked or another platform's."}
        return {"signed_in": False, "error": f"JFrog answered {r.status or r.error}."}

    # -- sign in / out ------------------------------------------------------------------------

    def sign_in(self, tool: str, body: dict) -> dict:
        if tool == "jfrog":
            return self._jfrog_sign_in(body)
        if tool not in ("gcp", "azure"):
            raise SettingsError(f"no sign-in for {tool}", 404)
        with self._lock:
            p = self._signin.get(tool)
            if p and not p["session"].done:
                p["session"].cancel()
        sh = self.shell_factory()
        if tool == "gcp":
            argv = ["gcloud", "auth", "login", "--no-launch-browser", "--update-adc", "--brief"]
            extra, saved = self._env_for("gcp"), {"mode": "console"}
        else:
            tenant = str(body.get("tenant") or "").strip()
            if tenant and not GUID_OR_DOMAIN.match(tenant):
                raise SettingsError("tenant must be a tenant ID or a domain such as contoso.onmicrosoft.com")
            argv = ["az", "login", "--use-device-code", "--allow-no-subscriptions", "-o", "none"]
            argv += ["--tenant", tenant] if tenant else []
            extra, saved = self._env_for("azure"), {"mode": "console", **({"tenant": tenant} if tenant else {})}
        sess = sh.start(argv, timeout=PENDING_SECONDS, extra_env=extra)
        with self._lock:
            self._signin[tool] = {"session": sess, "started": self.clock()}

        def finish():
            if sess.wait() == 0 and saved is not None:
                if tool == "gcp":
                    self._carry_gcp_project()
                self._update(tool, saved)
                self.apply()
        threading.Thread(target=finish, daemon=True, name=f"signin-{tool}").start()
        return {"started": True}

    def _carry_gcp_project(self) -> None:
        """A new gcloud login has no project: start from the one this machine's login uses."""
        sh = self.shell_factory()
        console = {"CLOUDSDK_CONFIG": str(self.dir_of("gcp"))}
        if sh.run(["gcloud", "config", "get-value", "project"], timeout=60, extra_env=console).stdout.strip():
            return
        machine = self._machine.get("CLOUDSDK_CONFIG") or str(Path.home() / ".config" / "gcloud")
        project = sh.run(["gcloud", "config", "get-value", "project"], timeout=60,
                         extra_env={"CLOUDSDK_CONFIG": machine}).stdout.strip()
        if project and PROJECT_RE.match(project):
            sh.run(["gcloud", "config", "set", "project", project], timeout=60, extra_env=console)

    def send_code(self, tool: str, code: str) -> dict:
        p = self._signin.get(tool)
        if not p or p["session"].done:
            raise SettingsError("no sign-in is waiting for a code", 409)
        code = str(code or "").strip()
        if not code or len(code) > 2048 or any(c.isspace() for c in code):
            raise SettingsError("paste the code exactly as shown")
        p["session"].send(code)
        return {"sent": True}

    def cancel(self, tool: str) -> dict:
        p = self._signin.pop(tool, None)
        if p:
            p["session"].cancel()
        return {"cancelled": bool(p)}

    def sign_out(self, tool: str) -> dict:
        """Back to this machine's own login. The console's own sign-in is signed out and removed."""
        if tool == "jfrog":
            self._jfrog_token, self._jfrog_login = "", "machine"
            self.apply()
            return {"signed_out": True}
        if tool not in ("gcp", "azure"):
            raise SettingsError(f"nothing to sign out for {tool}", 404)
        self.cancel(tool)
        d = self.dir_of(tool)
        if d.is_dir():
            sh = self.shell_factory()
            argv = ["gcloud", "auth", "revoke", "--all", "--quiet"] if tool == "gcp" else ["az", "logout"]
            sh.run(argv, timeout=60, extra_env={"gcp": {"CLOUDSDK_CONFIG": str(d)}, "azure": {"AZURE_CONFIG_DIR": str(d)}}[tool])
            shutil.rmtree(d, ignore_errors=True)
        keep = {k: v for k, v in (self.settings().get(tool) or {}).items() if k != "mode"}
        self._update(tool, keep or None)
        self.apply()
        return {"signed_out": True}

    def _jfrog_sign_in(self, body: dict) -> dict:
        url = str(body.get("url") or (self.settings().get("jfrog") or {}).get("url") or "").strip().rstrip("/")
        if not URL_RE.match(url + "/"):
            raise SettingsError("JFrog URL must be the platform address, e.g. https://acme.jfrog.io")
        tok = str(body.get("token") or "").strip()
        jf = dict(self.settings().get("jfrog") or {})
        jf["url"] = url
        self._update("jfrog", jf)
        if body.get("browser"):
            return self._jfrog_browser(url)
        if not tok:
            return {"saved": True}
        if len(tok) < 16 or len(tok) > 8192 or any(c.isspace() for c in tok):
            raise SettingsError("that does not look like a JFrog access token")
        check = self._check_jfrog(self.shell_factory(), url, tok)
        if not check.get("signed_in"):
            raise SettingsError(check.get("error") or "JFrog did not accept the token")
        self._jfrog_token, self._jfrog_login = tok, "console"
        self.apply()
        return {"saved": True, "admin": check.get("admin")}

    def _jfrog_browser(self, url: str) -> dict:
        """JFrog's browser login (as `jf login` does): register a session, the operator signs in on the
        page it names, and the token is fetched for that session. The token stays in memory only."""
        with self._lock:
            p = self._signin.pop("jfrog", None)
        if p:
            p["session"].cancel()
        sid, sh = self.jfrog_session_id(), self.shell_factory()
        base = f"{url}/access/api/v2/authentication/jfrog_client_login"
        r = sh.http("POST", f"{base}/request", body=json.dumps({"session": sid}).encode(),
                    headers={"Content-Type": "application/json"})
        if not r.ok:
            raise SettingsError(f"JFrog did not start a browser sign-in ({r.status or r.error})", 502)
        sess = _JfrogLogin(f"{url}/ui/login?jfClientSession={sid}&jfClientName=JFrog-Console&jfClientCode=1", sid[-4:])
        with self._lock:
            self._signin["jfrog"] = {"session": sess, "started": self.clock()}

        def poll():
            deadline = time.monotonic() + PENDING_SECONDS
            while not sess.cancelled and time.monotonic() < deadline:
                res = sh.http("GET", f"{base}/token/{sid}", sensitive=True)
                data = res.json() if res.ok else None
                tok = data.get("access_token") if isinstance(data, dict) else None
                if tok:
                    check = self._check_jfrog(sh, url, tok)
                    if check.get("signed_in"):
                        self._jfrog_token, self._jfrog_login = tok, "console"
                        self.apply()
                        sess.finish(0)
                    else:
                        sess.finish(1, check.get("error") or "JFrog did not accept the token")
                    return
                time.sleep(self.jfrog_poll)
            sess.finish(1, "cancelled" if sess.cancelled else "the browser sign-in was not completed in time")
        threading.Thread(target=poll, daemon=True, name="signin-jfrog").start()
        return {"started": True, "url": sess.url, "code": sess.code}

    # -- plain choices ------------------------------------------------------------------------

    def set_values(self, tool: str, body: dict) -> dict:
        if tool == "ado":
            vals = {}
            for k in ADO_KEYS:
                v = str(body.get(k) or "").strip()
                if v and not NAME_RE.match(v):
                    raise SettingsError(f"{k.replace('_', ' ')}: letters, digits, spaces, dots, dashes, underscores")
                if v:
                    vals[k] = v
            missing = [k for k in ADO_KEYS[:3] if k not in vals]
            if missing:
                raise SettingsError(f"needs {', '.join(m.replace('_', ' ') for m in missing)}")
            self._update("ado", vals)
            if self.on_ado:
                self.on_ado(vals)
            return {"saved": True}
        if tool == "azure":
            sub = str(body.get("subscription") or "").strip()
            if not re.match(r"^[0-9a-fA-F-]{36}$", sub):
                raise SettingsError("pick a subscription")
            r = self.shell_factory().run(["az", "account", "set", "--subscription", sub], timeout=60)
            if not r.ok:
                raise SettingsError("Azure did not switch to that subscription: " + redact.text(r.stderr.strip())[:200])
            return {"saved": True}
        if tool == "gcp":
            project = str(body.get("project") or "").strip()
            if not PROJECT_RE.match(project):
                raise SettingsError("pick a project")
            r = self.shell_factory().run(["gcloud", "config", "set", "project", project, "--quiet"], timeout=60)
            if not r.ok:
                raise SettingsError("gcloud did not switch to that project: " + redact.text(r.stderr.strip())[:200])
            return {"saved": True}
        raise SettingsError(f"no settings for {tool}", 404)

    def choices(self, tool: str) -> list:
        """Live pickers: Azure subscriptions, Google Cloud projects."""
        sh = self.shell_factory()
        if tool == "azure":
            r = sh.run(["az", "account", "list", "-o", "json"], timeout=60)
            return [{"id": a.get("id"), "name": a.get("name"), "tenant": a.get("tenantId")}
                    for a in (r.json([]) or []) if a.get("id")] if r.ok else []
        if tool == "gcp":
            r = sh.run(["gcloud", "projects", "list", "--format=json(projectId,name)", "--limit=500"], timeout=60)
            return [{"id": p.get("projectId"), "name": p.get("name") or p.get("projectId")}
                    for p in (r.json([]) or []) if p.get("projectId")] if r.ok else []
        raise SettingsError(f"no choices for {tool}", 404)


class _JfrogLogin:
    """A pending JFrog browser sign-in, shaped like a shell Session for the sign-in plumbing."""

    def __init__(self, url: str, code: str):
        self.url, self.code, self.lines = url, code, []
        self.done, self.rc, self.error, self.cancelled = False, None, "", False

    def finish(self, rc: int, error: str = "") -> None:
        self.rc, self.error, self.done = rc, error, True

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self):
        return self.rc
