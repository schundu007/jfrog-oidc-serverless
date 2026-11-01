"""Verify (spec §4, step 8): prove the live workload exchanges its own identity for a JFrog token.

Read-only: nothing is deployed, executed or changed. Per kind:
- Cloud Run service / function, Container App, Functions (Premium, Flex, on Container Apps): call the
  workload's /healthz/jfrog self-test (claims, exchange, artifact read). Cloud Run takes the operator's
  identity token; Functions take a function key read from Azure. Both stay in memory: the calls that
  print them are sensitive (never logged or recorded) and neither is ever returned.
- Cloud Run worker pool: its latest `jfrog_check` log entry (it checks every few minutes).
- Cloud Run job, Container Apps job: the logs of the last execution (run by Build and deploy).

The result holds the JFrog user, the identity, the token's lifetime and the artifact read; never a
token, a key or the artifact's content.
"""

from __future__ import annotations

import json
import time

from .. import model

GCP_ID_TOKEN = ["gcloud", "auth", "print-identity-token"]
EXCHANGED = "exchanged identity for a JFrog token"
WAKE_TRIES, WAKE_WAIT = 4, 15
POOL_POLL, POOL_WAIT = 20, 420
sleep = time.sleep   # seam for tests


def _g(cfg):
    t = model.get_path(cfg, "target.gcp") or {}
    return t.get("name", ""), t.get("region", ""), t.get("project", "")


def _a(cfg):
    t = model.get_path(cfg, "target.azure") or {}
    return t.get("name", ""), t.get("resource_group", ""), t.get("subscription", "")


# -- the exact commands (tests build their fixtures from these) --------------------------------------

def gcp_url_argv(cfg):
    n, r, p = _g(cfg)
    return ["gcloud", "run", "services", "describe", n, "--region", r, "--project", p, "--format=value(status.url)"]


def worker_pool_logs_argv(cfg):
    n, _, p = _g(cfg)
    f = (f'resource.type="cloud_run_worker_pool" AND resource.labels.worker_pool_name="{n}" '
         f'AND jsonPayload.event="jfrog_check"')
    return ["gcloud", "logging", "read", f, "--project", p, "--freshness=1h", "--limit=1", "--format=json"]


def gcp_executions_argv(cfg):
    n, r, p = _g(cfg)
    return ["gcloud", "run", "jobs", "executions", "list", "--job", n, "--region", r, "--project", p,
            "--limit=1", "--format=json"]


def gcp_job_logs_argv(cfg, execution):
    n, _, p = _g(cfg)
    f = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{n}" '
         f'AND labels."run.googleapis.com/execution_name"="{execution}"')
    return ["gcloud", "logging", "read", f, "--project", p, "--freshness=30d", "--limit=50", "--format=json"]


def aca_fqdn_argv(cfg):
    n, rg, s = _a(cfg)
    return ["az", "containerapp", "show", "-g", rg, "-n", n, "--subscription", s,
            "--query", "properties.configuration.ingress.fqdn", "-o", "tsv", "--only-show-errors"]


def aca_revision_argv(cfg):
    n, rg, s = _a(cfg)
    return ["az", "containerapp", "revision", "list", "-g", rg, "-n", n, "--subscription", s,
            "--query", "[?properties.active].name | [0]", "-o", "tsv", "--only-show-errors"]


def aca_keys_argv(cfg, revision):
    n, rg, s = _a(cfg)
    return ["az", "containerapp", "function", "keys", "list", "--key-type", "hostKey", "--revision", revision,
            "-g", rg, "-n", n, "--subscription", s, "-o", "json", "--only-show-errors"]


def _site_id(cfg):
    n, rg, s = _a(cfg)
    return f"/subscriptions/{s}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{n}"


def site_host_argv(cfg):
    # `az functionapp show` returns no host name for Flex Consumption; the resource itself has it.
    return ["az", "resource", "show", "--ids", _site_id(cfg), "--query", "properties.defaultHostName", "-o", "tsv",
            "--only-show-errors"]


def site_key_argv(cfg):
    return ["az", "rest", "--method", "post", "--url",
            f"https://management.azure.com{_site_id(cfg)}/host/default/listkeys?api-version=2023-12-01",
            "--query", "functionKeys.default", "-o", "tsv", "--only-show-errors"]


def caj_executions_argv(cfg):
    n, rg, s = _a(cfg)
    return ["az", "containerapp", "job", "execution", "list", "-g", rg, "-n", n, "--subscription", s, "-o", "json",
            "--only-show-errors"]


def caj_env_argv(cfg):
    n, rg, s = _a(cfg)
    return ["az", "containerapp", "job", "show", "-g", rg, "-n", n, "--subscription", s,
            "--query", "properties.environmentId", "-o", "tsv", "--only-show-errors"]


def workspace_argv(env_id):
    return ["az", "containerapp", "env", "show", "--ids", env_id,
            "--query", "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId", "-o", "tsv",
            "--only-show-errors"]


def caj_logs_argv(workspace, execution):
    q = (f"ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '{execution}' "
         f"| project TimeGenerated, Log_s | order by TimeGenerated asc | take 50")
    return ["az", "monitor", "log-analytics", "query", "-w", workspace, "--analytics-query", q, "-o", "json",
            "--only-show-errors"]


# -- results -----------------------------------------------------------------------------------------

def _result(kind, method, status, **kw):
    out = {"status": status, "kind": kind, "method": method, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out.update({k: v for k, v in kw.items() if v not in (None, "")})
    return out


def _fail(kind, method, reason, **kw):
    return _result(kind, method, "fail", reason=reason, **kw)


def _lifetime(seconds):
    try:
        return f"{int(seconds) // 60} min"
    except (TypeError, ValueError):
        return None


def _identity(claims):
    claims = claims if isinstance(claims, dict) else {}
    return claims.get("email") or claims.get("oid") or claims.get("appid") or claims.get("sub")


def _from_selftest(kind, where, status, body):
    try:
        st = json.loads(body) if body else {}
    except ValueError:
        st = {}
    if not isinstance(st, dict) or not st:
        return _fail(kind, "health endpoint", f"/healthz/jfrog answered HTTP {status or 'nothing'}", where=where)
    check = st.get("check") if isinstance(st.get("check"), dict) else {}
    read = st.get("read") if isinstance(st.get("read"), dict) else None
    read_txt = f"{read.get('path')}: {read.get('status') or read.get('error')}" if read else None
    common = dict(where=where, identity=_identity(st.get("claims")), jfrog_user=check.get("username"),
                  token_lifetime=_lifetime(check.get("expires_in")), read=read_txt)
    if st.get("ok") and check.get("username"):
        return _result(kind, "health endpoint", "pass", **common)
    reason = check.get("error") or (read and read.get("error")) or f"the self-test answered HTTP {status}"
    return _fail(kind, "health endpoint", f"exchange failed: {reason}", **common)


def _get(sh, kind, url, headers=None, fresh=False):
    if fresh:   # the workload drops its cached JFrog token and exchanges again
        url += "?fresh=1"
    res = sh.http("GET", url, headers=headers or {}, timeout=90, sensitive=False)
    if res.status == 0:
        return _fail(kind, "health endpoint", f"no answer from {url}: {res.error}", where=url)
    if res.status in (401, 403) and not res.body.strip().startswith("{"):
        return _fail(kind, "health endpoint", f"{url} refused the call (HTTP {res.status})", where=url)
    return _from_selftest(kind, url, res.status, res.body)


def _cloud_run_http(cfg, sh, kind, opts):
    r = sh.run(gcp_url_argv(cfg), timeout=60)
    url = r.stdout.strip()
    if not r.ok or not url:
        return _fail(kind, "health endpoint", f"no URL for the service: {r.error_text() or 'not deployed'}")
    tok = sh.run(GCP_ID_TOKEN, timeout=60, sensitive=True)
    if not tok.ok or not tok.stdout.strip():
        return _fail(kind, "health endpoint", "could not get an identity token for the call (gcloud auth)")
    try:
        return _get(sh, kind, url.rstrip("/") + "/healthz/jfrog", {"Authorization": "Bearer " + tok.stdout.strip()},
                    opts.get("fresh"))
    finally:
        del tok


def _container_app(cfg, sh, kind, opts):
    r = sh.run(aca_fqdn_argv(cfg), timeout=90)
    fqdn = r.stdout.strip()
    if not r.ok or not fqdn:
        return _fail(kind, "health endpoint", f"no ingress host: {r.error_text() or 'ingress is off'}")
    return _get(sh, kind, f"https://{fqdn}/healthz/jfrog", fresh=opts.get("fresh"))


def _first_value(obj):
    """The first key value in an az keys answer ({"keys": [{"value": ..}]}, a list, or {"default": ..})."""
    if isinstance(obj, dict):
        if isinstance(obj.get("value"), str):
            return obj["value"]
        for k in ("keys", "functionKeys", "value"):
            v = obj.get(k)
            if isinstance(v, (list, dict)):
                found = _first_value(v)
                if found:
                    return found
        if isinstance(obj.get("default"), str):
            return obj["default"]
    if isinstance(obj, list):
        for x in obj:
            found = _first_value(x)
            if found:
                return found
    return None


def _functions_aca(cfg, sh, kind, opts):
    r = sh.run(aca_fqdn_argv(cfg), timeout=90)
    fqdn = r.stdout.strip()
    if not r.ok or not fqdn:
        return _fail(kind, "health endpoint", f"no ingress host: {r.error_text() or 'ingress is off'}")
    rev = sh.run(aca_revision_argv(cfg), timeout=90).stdout.strip()
    for attempt in range(WAKE_TRIES):
        keys = sh.run(aca_keys_argv(cfg, rev), timeout=180, sensitive=True)
        data = keys.json({})
        key = _first_value(data) if keys.ok else None
        # Scaled to zero: Azure lists keys only from a running replica. A plain request wakes one.
        if key or "Replicas set to 0" not in (keys.stdout + keys.stderr) or attempt == WAKE_TRIES - 1:
            break
        sh.http("GET", f"https://{fqdn}/", headers={}, timeout=90, sensitive=False)
        sleep(WAKE_WAIT)
    if not key:
        err = data.get("error") if isinstance(data, dict) else None
        why = (err.get("message") if isinstance(err, dict) else None) or keys.error_text(200) or "no key returned"
        return _fail(kind, "health endpoint", f"Azure could not list the function keys: {why}", where=f"https://{fqdn}")
    try:
        return _get(sh, kind, f"https://{fqdn}/api/healthz/jfrog", {"x-functions-key": key}, opts.get("fresh"))
    finally:
        del key, keys, data


def _function_app(cfg, sh, kind, opts):
    r = sh.run(site_host_argv(cfg), timeout=90)
    host = r.stdout.strip()
    if not r.ok or not host:
        return _fail(kind, "health endpoint", f"no host name for the function app: {r.error_text() or 'not deployed'}")
    k = sh.run(site_key_argv(cfg), timeout=90, sensitive=True)
    key = k.stdout.strip()
    if not k.ok or not key:
        return _fail(kind, "health endpoint", "Azure could not list the function keys", where=f"https://{host}")
    try:
        return _get(sh, kind, f"https://{host}/api/healthz/jfrog", {"x-functions-key": key}, opts.get("fresh"))
    finally:
        del key, k


def _worker_pool(cfg, sh, kind, opts):
    # With opts["since"], wait for a check logged after that time (the worker checks every minute).
    since, waited = opts.get("since"), 0
    while True:
        r = sh.run(worker_pool_logs_argv(cfg), timeout=90)
        entries = r.json([]) if r.ok else None
        if entries is None:
            return _fail(kind, "workload logs", f"could not read the logs: {r.error_text()}")
        if not since or (entries and str(entries[0].get("timestamp") or "") > since) or waited >= POOL_WAIT:
            break
        sleep(POOL_POLL)
        waited += POOL_POLL
    if since and not (entries and str(entries[0].get("timestamp") or "") > since):
        return _fail(kind, "workload logs", f"no JFrog check logged since {since}")
    if not entries:
        return _fail(kind, "workload logs", "no JFrog check logged in the last hour; is the worker pool running?")
    e = entries[0]
    p = e.get("jsonPayload") or {}
    check = p.get("check") if isinstance(p.get("check"), dict) else {}
    common = dict(where="worker pool logs", when=e.get("timestamp"), identity=_identity(p.get("claims")),
                  jfrog_user=check.get("username"), token_lifetime=_lifetime(check.get("expires_in")))
    if p.get("ok") and check.get("username"):
        return _result(kind, "workload logs", "pass", **common)
    return _fail(kind, "workload logs", f"the latest check failed: {p.get('error') or check.get('error') or 'not ok'}", **common)


def _from_job_lines(kind, lines, where, when=None, identity=None):
    lines = [str(x) for x in lines if x]
    exchanged = next((x for x in lines if x.startswith(EXCHANGED)), None)
    read = next((x for x in lines if x.startswith("read ")), None)
    failed = next((x for x in lines if x.startswith("FAILED")), None)
    lifetime = None
    if exchanged:
        tail = exchanged[len(EXCHANGED):].split()
        secs = next((t.rstrip("s") for t in tail if t.rstrip("s").isdigit()), None)
        lifetime = _lifetime(secs)
    common = dict(where=where, when=when, identity=identity, token_lifetime=lifetime,
                  read=read[5:].split(":", 1)[0] if read else None)
    if exchanged and read and not failed:
        return _result(kind, "last job run", "pass", **common)
    return _fail(kind, "last job run", failed or "the run logged no JFrog exchange and read", **common)


def _gcp_job(cfg, sh, kind, opts=None):
    r = sh.run(gcp_executions_argv(cfg), timeout=90)
    ex = r.json([]) if r.ok else None
    if ex is None:
        return _fail(kind, "last job run", f"could not list the job's runs: {r.error_text()}")
    if not ex:
        return _fail(kind, "last job run", "the job has not run yet; run its Build and deploy pipeline, then verify")
    e = ex[0]
    name = (e.get("metadata") or {}).get("name") or e.get("name", "")
    st = e.get("status") or {}
    sa = (((e.get("spec") or {}).get("template") or {}).get("spec") or {}).get("serviceAccountName")
    if not st.get("succeededCount"):
        return _fail(kind, "last job run", f"the last run ({name}) did not succeed", where=name, identity=sa)
    logs = sh.run(gcp_job_logs_argv(cfg, name), timeout=120)
    lines = [x.get("textPayload") for x in (logs.json([]) if logs.ok else [])]
    return _from_job_lines(kind, lines, name, st.get("completionTime"), sa)


def _azure_job(cfg, sh, kind, opts=None):
    r = sh.run(caj_executions_argv(cfg), timeout=120)
    ex = r.json([]) if r.ok else None
    if ex is None:
        return _fail(kind, "last job run", f"could not list the job's runs: {r.error_text()}")
    if not ex:
        return _fail(kind, "last job run", "the job has not run yet; run its Build and deploy pipeline, then verify")
    e = max(ex, key=lambda x: (x.get("properties") or {}).get("startTime") or "")
    name, props = e.get("name", ""), e.get("properties") or {}
    if props.get("status") != "Succeeded":
        return _fail(kind, "last job run", f"the last run ({name}) is {props.get('status') or 'unknown'}", where=name)
    env = sh.run(caj_env_argv(cfg), timeout=90).stdout.strip()
    ws = sh.run(workspace_argv(env), timeout=90).stdout.strip() if env else ""
    if not ws:
        return _fail(kind, "last job run", "the job's environment sends no logs to Log Analytics", where=name)
    logs = sh.run(caj_logs_argv(ws, name), timeout=180)
    rows = logs.json([]) if logs.ok else []
    if not rows:
        return _fail(kind, "last job run", "the run's logs have not reached Log Analytics yet; verify again in a few minutes",
                     where=name, when=props.get("startTime"))
    return _from_job_lines(kind, [x.get("Log_s") for x in rows], name, props.get("startTime"))


METHODS = {
    "gcp_run_service": _cloud_run_http,
    "gcp_run_function": _cloud_run_http,
    "gcp_run_worker_pool": _worker_pool,
    "gcp_run_job": _gcp_job,
    "azure_container_app": _container_app,
    "azure_container_app_job": _azure_job,
    "azure_functions_aca": _functions_aca,
    "azure_functions_premium": _function_app,
    "azure_functions_flex": _function_app,
}


def run(cfg: dict, sh, fresh: bool = False, since: str | None = None) -> dict:
    """fresh: the workload exchanges again instead of answering from its cached token (Prove).
    since: a worker pool's check must be logged after this time."""
    kind = model.get_path(cfg, "target.kind")
    fn = METHODS.get(kind)
    if fn is None:
        return _result(kind, "none", "skipped", reason="no live check for this kind yet (runtime path only)")
    return fn(cfg, sh, kind, {"fresh": fresh, "since": since})
