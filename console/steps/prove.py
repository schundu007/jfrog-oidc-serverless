"""Prove: the workload reads JFrog only through its OIDC trust, the same proof the KCP console gives a cluster.

1. No stored secret: no JFrog token, password or API key (nor a registry password) in the workload's
   settings. Names only: a value is never read except the JFrog URL, provider and artifact path.
2. Download with OIDC: the workload exchanges again (?fresh=1, a new job run, or the worker pool's next
   check) and reads its artifact.
3. No long-lived token: the mapped JFrog user holds no token living longer than a day.
4. JFrog records the download: the artifact's "last downloaded by" is the mapped user.
5. Trust removed (after the operator's approval, bound to the mapping): the identity mapping is saved,
   then deleted.
6. Download refused: a fresh exchange is refused.
7. Trust restored: always runs once the mapping was removed, even after a failure or a cancel.
8. Download works again.

Never a token, key or artifact content in the result: JFrog calls use the console operator's own
short-lived token (jfrog_operator), and workload calls reuse Verify.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .. import gcprun, model
from ..jfrog_operator import OperatorError
from . import verify, workload_logs

ROWS = [("secret", "No stored secret"), ("granted", "Download with OIDC"), ("keys", "No long-lived JFrog token"),
        ("who", "JFrog records the download"), ("break", "Trust removed"), ("refused", "Download refused"),
        ("restore", "Trust restored"), ("repull", "Download works again")]
SECRET_NAME = re.compile(r"^(JFROG|ARTIFACTORY|JF|RT)_\w*(TOKEN|PASSWORD|PASSWD|API_?KEY|SECRET)$|^DOCKER_REGISTRY_SERVER_PASSWORD$",
                         re.I)
KEEP = ("JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_HEALTHCHECK_PATH", "JFROG_ARTIFACT_PATH")
LONG_LIVED = 86400
JOB_KINDS = {"gcp_run_job", "azure_container_app_job"}
JOB_POLL, JOB_WAIT, LOG_TRIES, LOG_WAIT = 15, 900, 12, 30
sleep = time.sleep   # seam for tests


class StepError(RuntimeError):
    pass


# -- commands ----------------------------------------------------------------------------------------

def settings_argv(cfg):
    kind = model.get_path(cfg, "target.kind")
    if kind.startswith("gcp_"):
        n, r, p = verify._g(cfg)
        return [*gcprun.group(gcprun.RESOURCE.get(kind, "services")), "describe", n, "--region", r, "--project", p,
                "--format=json"]
    n, rg, s = verify._a(cfg)
    loc = ["-g", rg, "-n", n, "--subscription", s]
    if kind == "azure_container_app_job":
        return ["az", "containerapp", "job", "show", *loc, "-o", "json", "--only-show-errors"]
    if kind in ("azure_container_app", "azure_functions_aca"):
        return ["az", "containerapp", "show", *loc, "-o", "json", "--only-show-errors"]
    return ["az", model.az_site(kind), "config", "appsettings", "list", *loc, "-o", "json", "--only-show-errors"]


def gcp_execute_argv(cfg):
    n, r, p = verify._g(cfg)
    return ["gcloud", "run", "jobs", "execute", n, "--region", r, "--project", p, "--wait", "--format=json"]


def caj_start_argv(cfg):
    n, rg, s = verify._a(cfg)
    return ["az", "containerapp", "job", "start", "-g", rg, "-n", n, "--subscription", s, "-o", "json", "--only-show-errors"]


def read_settings(cfg, sh) -> dict:
    """{names, values (KEEP only), registry_passwords}. App settings print values: that call is sensitive and
    everything but the names and KEEP is dropped unread."""
    kind = model.get_path(cfg, "target.kind")
    webapp = kind in model.SITE_KINDS
    r = sh.run(settings_argv(cfg), timeout=120, sensitive=webapp)
    data = r.json(None) if r.ok else None
    if data is None:
        raise StepError(f"could not read the workload's settings: {r.error_text(200) if not webapp else 'az failed'}")
    regs = 0
    if webapp:
        pairs = [(s.get("name"), s.get("value")) for s in data if isinstance(s, dict)]
    elif kind.startswith("gcp_"):
        spec = gcprun.pod_spec(data, gcprun.RESOURCE.get(kind, "services"))
        pairs = [(e.get("name"), e.get("value")) for c in spec.get("containers") or [] for e in c.get("env") or []]
    else:
        props = data.get("properties") or {}
        pairs = [(e.get("name"), e.get("value")) for c in (props.get("template") or {}).get("containers") or []
                 for e in c.get("env") or []]
        regs = sum(1 for g in (props.get("configuration") or {}).get("registries") or [] if g.get("passwordSecretRef"))
    names = sorted({n for n, _ in pairs if n})
    values = {n: v for n, v in pairs if n in KEEP and v}
    del data, pairs
    return {"names": names, "values": values, "registry_passwords": regs}


# -- the proof ---------------------------------------------------------------------------------------

class Prove:
    """approve(info) blocks until the operator approves removing the mapping (raises when cancelled);
    update(rows, facts) is called after every change. save_dir keeps the removed mapping until restored."""

    def __init__(self, cfg, sh, operator_for, approve, update, save_dir, log=None, clock=time.time):
        self.cfg, self.sh, self.operator_for = cfg, sh, operator_for
        self.approve, self.update, self.save_dir = approve, update, Path(save_dir)
        self.log, self.clock = log or (lambda line: None), clock
        self.kind = model.get_path(cfg, "target.kind")
        self.rows = [{"id": i, "title": t, "status": "pending", "text": ""} for i, t in ROWS]
        self.facts: dict = {}
        self.jf = None

    # -- bookkeeping
    def _row(self, rid):
        return next(r for r in self.rows if r["id"] == rid)

    def _set(self, rid, status, text="", **facts):
        self._row(rid).update(status=status, text=text)
        if facts:
            self._row(rid)["facts"] = facts
        self.update(self.rows, self.facts)

    def _do(self, rid, fn):
        self._set(rid, "running")
        try:
            out = fn()
        except (StepError, OperatorError) as exc:
            self._set(rid, "failed", str(exc))
            return False
        status, text, *facts = out
        self._set(rid, status, text, **(facts[0] if facts else {}))
        return status != "failed"

    def run(self) -> dict:
        try:
            ok = all(self._do(r, f) for r, f in (("secret", self.s_secret), ("granted", self.s_granted),
                                                    ("keys", self.s_keys), ("who", self.s_who)))
            if ok:
                ok = self._do("break", self.s_break)
                if ok:
                    ok = self._do("refused", self.s_refused)
        finally:
            if self.facts.get("removed") or (self.save_dir / "removed-mapping.json").exists():
                self._do("restore", self.s_restore)
            for r in self.rows:
                if r["status"] == "pending" and r["id"] != "repull":
                    r["status"], r["text"] = "skipped", "not reached"
        if self._row("restore")["status"] == "done":
            self._do("repull", self.s_repull)
        else:
            self._row("repull").update(status="skipped", text="trust not restored" if self.facts.get("removed") else "not reached")
        self._read_log()
        self.update(self.rows, self.facts)
        failed = [r["title"] for r in self.rows if r["status"] == "failed"]
        return {"proved": not failed and all(r["status"] in ("ok", "done", "warn") for r in self.rows),
                "failed": failed, "rows": self.rows, "facts": self.facts}

    # -- workload checks
    def _check(self, expect_ok: bool, since=None):
        """One fresh check of the workload; jobs run once first (that is the approved write for jobs)."""
        if self.kind in JOB_KINDS:
            self._run_job()
        res = verify.run(self.cfg, self.sh, fresh=True, since=since)
        tries = 0
        while self.kind == "azure_container_app_job" and res["status"] == "fail" and "not reached" in res.get("reason", "") \
                and tries < LOG_TRIES:
            sleep(LOG_WAIT)
            tries += 1
            res = verify.run(self.cfg, self.sh)
        return res

    def _answer(self, stage, res):
        """JFrog's answer to the workload at this stage, as the check saw it."""
        a = workload_logs.answer_from_check(res)
        self.facts.setdefault("answers", []).append({"stage": stage, "at": self._now(), **a})

    def _read_log(self):
        """The workload's own live log for the whole proof window (read-only; never fails the proof)."""
        if not self.facts.get("started") or not workload_logs.supported(self.cfg):
            return
        try:
            got = workload_logs.read(self.cfg, self.sh, self.facts["started"])
        except Exception as exc:   # a log read must never leave the trust removed or the run unfinished
            self.log(f"workload log not read: {exc}")
            got = None
        self.facts["workload_log"] = got or {"source": "workload log", "rows": [], "answers": [], "jfrog": None,
                                             "error": "the workload's log was not readable"}

    def _run_job(self):
        if self.kind == "gcp_run_job":
            r = self.sh.run(gcp_execute_argv(self.cfg), timeout=JOB_WAIT + 60)
            self.log("job run finished" if r.ok else "job run failed")
            return
        r = self.sh.run(caj_start_argv(self.cfg), timeout=120)
        name = (r.json({}) or {}).get("name") if r.ok else None
        if not name:
            raise StepError(f"could not start the job: {r.error_text(200)}")
        waited = 0
        while waited < JOB_WAIT:
            ex = self.sh.run(verify.caj_executions_argv(self.cfg), timeout=120).json([]) or []
            st = next(((e.get("properties") or {}).get("status") for e in ex if e.get("name") == name), None)
            if st in ("Succeeded", "Failed", "Stopped", "Degraded"):
                return
            sleep(JOB_POLL)
            waited += JOB_POLL
        raise StepError(f"the job run {name} did not finish in {JOB_WAIT // 60} min")

    def _now(self):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))

    # -- steps
    def s_secret(self):
        s = read_settings(self.cfg, self.sh)
        bad = [n for n in s["names"] if SECRET_NAME.search(n)]
        v = s["values"]
        self.facts.update(provider=v.get("JFROG_OIDC_PROVIDER") or "", jfrog_url=v.get("JFROG_URL") or "",
                          artifact=v.get("JFROG_ARTIFACT_PATH") or v.get("JFROG_HEALTHCHECK_PATH")
                          or model.get_path(self.cfg, "verify.artifact_path") or "",
                          settings=len(s["names"]))
        facts = {"settings_checked": len(s["names"]), "secret_settings": len(bad), "registry_passwords": s["registry_passwords"]}
        if bad or s["registry_passwords"]:
            raise StepError("stored credentials: " + ", ".join(bad + ([f"{s['registry_passwords']} registry password(s)"]
                                                                         if s["registry_passwords"] else [])))
        if not self.facts["provider"] or not self.facts["jfrog_url"]:
            raise StepError("the workload is not wired: JFROG_URL and JFROG_OIDC_PROVIDER are not set")
        return "ok", f"{len(s['names'])} settings, none a JFrog token, password or key", facts

    def s_granted(self):
        self.facts["started"] = self._now()
        res = self._check(True, since=self.facts["started"])
        self.facts["granted"] = res
        self._answer("granted", res)
        if res["status"] != "pass":
            raise StepError(f"the workload could not read JFrog: {res.get('reason')}")
        mapping = f"{self.cfg['name']}-runtime"
        user = res.get("jfrog_user") or f"{self.facts['provider']}/{mapping}"
        self.facts.update(user=user, mapping=user.split("/", 1)[-1])
        read = res.get("read") or self.facts["artifact"]
        return "ok", f"exchanged as {user}" + (f"; read {read}" if read else ""), \
            {"jfrog_user": user, "identity": res.get("identity") or "", "token_lifetime": res.get("token_lifetime") or "",
             "read": read or ""}

    def _jfrog(self):
        if self.jf is None:
            host = self.facts["jfrog_url"].split("//", 1)[-1].strip("/")
            self.jf = self.operator_for(host)
        return self.jf

    def s_keys(self):
        status, data = self._jfrog().call("GET", "/access/api/v1/tokens")
        if status != 200:
            return "warn", f"JFrog's token list was not readable (HTTP {status})"
        toks = (data or {}).get("tokens") or [] if isinstance(data, dict) else []
        user = self.facts["user"]
        mine = [t for t in toks if str(t.get("subject", "")).endswith(user)]
        long_lived = [t for t in mine if not t.get("expiry") or t["expiry"] - t.get("issued_at", t["expiry"]) > LONG_LIVED]
        if long_lived:
            raise StepError(f"{user} holds {len(long_lived)} long-lived token(s): a stored token could read instead")
        return "ok", f"0 long-lived; {len(mine)} short-lived from the exchange", \
            {"long_lived": 0, "short_lived": len(mine)}

    def s_who(self):
        path = self.facts["artifact"]
        if not path:
            return "warn", "no artifact path set on the workload"
        status, data = self._jfrog().call("GET", f"/artifactory/api/storage/{path}?stats")
        if status != 200 or not isinstance(data, dict):
            return "warn", f"JFrog's download stats for {path} were not readable (HTTP {status})"
        by, at = data.get("lastDownloadedBy") or "", data.get("lastDownloaded")
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at / 1000)) if isinstance(at, (int, float)) else ""
        facts = {"artifact": path, "last_downloaded_by": by, "last_downloaded": when, "downloads": data.get("downloadCount")}
        self.facts["who"] = facts
        if by.endswith(self.facts["user"].split("/", 1)[-1]) or by == self.facts["user"]:
            return "ok", f"{path}: last downloaded by {by}", facts
        return "warn", f"{path}: last downloaded by {by or 'nobody'} (another reader since?)", facts

    def _mapping(self):
        prov, name = self.facts["provider"], self.facts["mapping"]
        status, data = self._jfrog().call("GET", f"/access/api/v1/oidc/{prov}/identity_mappings")
        if status != 200:
            raise StepError(f"identity mappings of {prov}: HTTP {status}")
        m = next((m for m in data or [] if m.get("name") == name), None)
        if not m:
            raise StepError(f"no identity mapping {name} on {prov}")
        return m

    def approval_info(self, m) -> dict:
        info = {"target": self.cfg["name"], "kind": self.kind, "jfrog": self.facts["jfrog_url"],
                "provider": self.facts["provider"], "mapping": m["name"], "claims": m.get("claims") or {}}
        info["sha256"] = hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()
        return info

    def s_break(self):
        m = self._mapping()
        self._set("break", "needs_approval", f"approve removing {m['name']} for a moment")
        self.approve(self.approval_info(m))
        self._set("break", "running")
        self.save_dir.mkdir(parents=True, exist_ok=True)
        saved = self.save_dir / "removed-mapping.json"
        saved.write_text(json.dumps({"provider": self.facts["provider"], "mapping": m}))
        saved.chmod(0o600)
        self.facts["removed"] = True
        status, _ = self._jfrog().call("DELETE", f"/access/api/v1/oidc/{self.facts['provider']}/identity_mappings/{m['name']}")
        if status not in (200, 204):
            self.facts["removed"] = False
            saved.unlink(missing_ok=True)
            raise StepError(f"remove mapping: HTTP {status}")
        self.facts["broken_at"] = self.clock()
        return "done", f"{m['name']} removed: JFrog no longer trusts this identity", {"mapping": m["name"]}

    def s_refused(self):
        res = self._check(False, since=self._now())
        self.facts["refused"] = res
        self._answer("refused", res)
        if res["status"] == "pass":
            raise StepError("the workload still read JFrog with its trust removed: something else grants access")
        return "ok", f"refused: {res.get('reason')}", {"answer": res.get("reason") or ""}

    def s_restore(self):
        saved = self.save_dir / "removed-mapping.json"
        rec = json.loads(saved.read_text()) if saved.exists() else None
        if not rec:
            return "skipped", "nothing was removed"
        m = rec["mapping"]
        body = {k: m[k] for k in ("name", "description", "claims", "token_spec", "priority") if k in m}
        status, _ = self._jfrog().call("POST", f"/access/api/v1/oidc/{rec['provider']}/identity_mappings", body)
        if status not in (200, 201, 409):
            raise StepError(f"restore mapping: HTTP {status}; the saved mapping is kept in the run folder")
        saved.unlink(missing_ok=True)
        off = int(self.clock() - self.facts["broken_at"]) if "broken_at" in self.facts else None
        return "done", f"{body['name']} restored" + (f" after {off} s" if off is not None else ""), {"trust_off_seconds": off}

    def s_repull(self):
        res = self._check(True, since=self._now())
        tries = 0
        while res["status"] != "pass" and tries < 3:   # JFrog can take a moment to apply the restored mapping
            sleep(10)
            tries += 1
            res = self._check(True, since=self._now())
        self._answer("repull", res)
        if res["status"] != "pass":
            raise StepError(f"still refused after the trust was restored: {res.get('reason')}")
        return "ok", f"exchanged as {res.get('jfrog_user') or self.facts['user']} again", {"read": res.get("read") or ""}

