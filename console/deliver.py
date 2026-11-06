"""Delivery through Azure DevOps: the console proposes, the pipeline applies.

publish()        renders this config's shared workspace into the ADO repo
                 (terraform/workspaces/<scope>/, module sources relative, remote
                 state from pipelines/delivery.json) and commits it. With
                 "merge": "direct" it pushes to the target branch; otherwise it
                 pushes a branch and opens a pull request, and merging it is the
                 approval. pipelines/<cloud>-workload-onboard.yml then plans,
                 applies and wires.
wire_workspace() runs in that pipeline after apply: it wires every target of
                 the workspace with the console's own Wire step, approving
                 exactly the diff it computed (the merged PR is the approval).

Git reaches ADO with the git credential helper (Git Credential Manager) when one
is set for dev.azure.com, else with the token from `az account get-access-token`
(a sensitive call: never logged or recorded), which reaches git only through
GIT_CONFIG_* environment variables, never argv.

    python -m console.deliver wire <workspace-dir> <outputs.json>
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import model, redact
from .shell import Shell
from .steps import tfgen
from .steps import wire as wiremod

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"   # Azure DevOps
REQUIRED = {"ado": ("organization", "project", "repository")}
STATE_FIELDS = {"gcs": ("bucket",), "azurerm": ("resource_group_name", "storage_account_name", "container_name")}
STATE_TYPES = {"gcp": "gcs", "azure": "azurerm"}
WORKSPACE_FILES = ("backend.tf.json", "console.json", "main.tf.json", "terraform.tfvars.json")
# The ADO pipeline running pipelines/<cloud>-workload-onboard.yml, by its name in ADO.
ONBOARD_PIPELINES = {"gcp": "Google Cloud - Onboard workloads to JFrog", "azure": "Azure - Onboard workloads to JFrog"}


def onboard_pipeline(cloud: str) -> str:
    return ONBOARD_PIPELINES.get(cloud, f"{cloud}-workload-onboard")


class DeliveryError(RuntimeError):
    pass


def pipeline_identity_settings(stacks_dir) -> dict:
    """Each cloud's pipeline identity and state backend, from its pipeline identity stack's outputs
    (terraform/stacks/serverless/<cloud>/<cloud>-pipeline-identity, local state). A cloud whose pipeline identity isn't deployed
    is left out."""
    def outputs(stack):
        try:
            return {k: v.get("value") for k, v in
                    json.loads((Path(stacks_dir) / stack / "terraform.tfstate").read_text())["outputs"].items()}
        except (OSError, ValueError, KeyError, AttributeError):
            return {}

    found = {"pipeline_identity": {}, "state": {}}
    g = outputs("serverless/gcp/gcp-pipeline-identity")
    if g.get("tfstate_bucket") and (g.get("gcp_service_accounts") or {}).get("setup"):
        found["pipeline_identity"]["gcp"] = g["gcp_service_accounts"]["setup"]
        found["state"]["gcp"] = {"type": "gcs", "bucket": g["tfstate_bucket"]}
    a = outputs("serverless/azure/azure-pipeline-identity")
    t = a.get("azure_tfstate") or {}
    if a.get("azure_setup_identity_principal_id") and t.get("storage_account"):
        found["pipeline_identity"]["azure"] = a["azure_setup_identity_principal_id"]
        found["state"]["azure"] = {"type": "azurerm", "resource_group_name": t["resource_group"],
                                   "storage_account_name": t["storage_account"], "container_name": t["container"]}
    return found


ADO_KEYS = ("organization", "project", "repository", "target_branch")


def ado_overrides(path) -> dict:
    """The Azure DevOps organization, project, repository and branch entered on Settings
    (.console/connections/settings.json, "ado"), or {}. Non-secret values only."""
    try:
        ado = json.loads(Path(path).read_text()).get("ado") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: str(ado[k]).strip() for k in ADO_KEYS if isinstance(ado.get(k), str) and ado[k].strip()}


def load_settings(path, stacks_dir=None, overrides=None) -> dict | None:
    """pipelines/delivery.json (the ADO repository and merge mode), or None when the console
    applies locally (no file, or no Azure DevOps organization and project in it or on Settings). Pipeline identities and state backends come from the pipeline identity stacks.
    overrides: the Settings file; its Azure DevOps values win over delivery.json's, and alone
    (no delivery.json) they are enough."""
    path = Path(path)
    over = ado_overrides(overrides) if overrides else {}
    if not path.is_file() and not over:
        return None
    data = json.loads(path.read_text()) if path.is_file() else {"merge": "direct", "environment": "test"}
    data["ado"] = {**(data.get("ado") or {}), **over}
    if not (data["ado"].get("organization") or data["ado"].get("project")):
        return None   # a new environment: local mode until Azure DevOps is entered on Settings
    for section, keys in REQUIRED.items():
        missing = [k for k in keys if not (data.get(section) or {}).get(k)]
        if missing:
            raise DeliveryError(f"{path.name}: {section} needs {', '.join(missing)}")
    data = Settings(data, stacks_dir or path.resolve().parent.parent / "terraform" / "stacks")
    refresh(data)
    return data


class Settings(dict):
    """The delivery settings, plus where to re-read the pipeline identity stacks' outputs from (refresh)."""

    def __init__(self, data: dict, stacks_dir):
        super().__init__(data)
        self.stacks_dir = Path(stacks_dir)
        self.file_values = {k: dict(data.get(k) or {}) for k in ("pipeline_identity", "state")}


def refresh(settings):
    """Re-read pipeline identities and state backends from the pipeline identity stacks, in place: a pipeline identity
    deployed (or destroyed) while the console runs counts at once, without a restart."""
    if isinstance(settings, Settings):
        found = pipeline_identity_settings(settings.stacks_dir)
        for key in ("pipeline_identity", "state"):
            settings[key] = {**settings.file_values[key], **found[key]}
    return settings


def _backend(settings: dict, cloud: str, ws_name: str) -> dict:
    b = dict((settings.get("state") or {}).get(cloud) or {})
    t = b.get("type")
    if t != STATE_TYPES.get(cloud):
        raise DeliveryError(f"delivery state for {cloud} must be remote ({STATE_TYPES.get(cloud, 'unknown cloud')}), "
                            f"not {t!r}")
    missing = [k for k in STATE_FIELDS[t] if not b.get(k)]
    if missing:
        raise DeliveryError(f"delivery state for {cloud} needs {', '.join(missing)}")
    if t == "gcs":
        return {"type": t, "bucket": b["bucket"], "prefix": f"workspaces/{ws_name}"}
    return {"type": t, **{k: b[k] for k in STATE_FIELDS[t]}, "key": f"workspaces/{ws_name}.tfstate"}


GONE_MARKERS = ("does not exist", "ResourceNotFound", "Request_ResourceNotFound")


def gone_targets(cfg: dict, wt, sh: Shell) -> list:
    """The workspace's other Azure targets whose managed identity no longer exists in Entra:
    their role assignments would fail the whole apply. Only a definite not-found counts;
    any other error keeps the target."""
    if model.cloud_of(cfg["target"]["kind"]) != "azure":
        return []
    state = tfgen.read_state(Path(wt) / "terraform", tfgen.workspace_key(cfg)) or {}
    gone = []
    for name, t in sorted((state.get("targets") or {}).items()):
        pid = t.get("principal_id")
        if name == cfg["name"] or not pid:
            continue
        res = sh.run(["az", "ad", "sp", "show", "--id", pid, "--query", "id", "-o", "tsv"], timeout=60.0,
                     meaning={0: "identity exists", 3: "no such identity"})
        if not res.ok and any(m in res.error_text() for m in GONE_MARKERS):
            gone.append(name)
    return gone


def _render(cfg: dict, wt, drop=()) -> dict:
    """Render into the worktree; returns the workspace path relative to it and its files."""
    rendered = tfgen.render(cfg, Path(wt) / "terraform", modules="../../modules", drop=drop)
    ws = Path(rendered["dir"])
    configs = ws / "configs"
    configs.mkdir(exist_ok=True)
    for name in drop:
        (configs / f"{name}.json").unlink(missing_ok=True)
    text = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
    if redact.text(text) != text:
        raise DeliveryError("refusing to commit the config: a value looks like a secret")
    (configs / f"{cfg['name']}.json").write_text(text)
    rel = ws.relative_to(Path(wt)).as_posix()
    files = [f"{rel}/{n}" for n in WORKSPACE_FILES] + [f"{rel}/configs/{cfg['name']}.json"]
    return {"workspace": rel, "files": files, "dropped": [f"{rel}/configs/{n}.json" for n in drop], "targets": rendered["targets"], "warnings": rendered["warnings"]}


def publish(cfg: dict, sh: Shell, repo_root, settings: dict, run_id: str, log=None) -> dict:
    log = log or (lambda _l: None)
    ado = settings["ado"]
    org_url = f"https://dev.azure.com/{ado['organization']}"
    repo_url = f"{org_url}/{ado['project']}/_git/{ado['repository']}"
    base = ado.get("target_branch") or "main"
    direct = settings.get("merge") == "direct"
    branch = f"onboard/{cfg['name']}-{run_id[:8]}"
    pipeline_url = f"{org_url}/{ado['project']}/_build"
    repo = str(Path(repo_root))
    wt = Path(repo_root) / ".console" / "delivery" / run_id

    c = copy.deepcopy(cfg)
    ws_name = tfgen.workspace_key(c).replace(":", "_")
    cloud = model.cloud_of(c["target"]["kind"])
    c["state_backend"] = _backend(settings, cloud, ws_name)

    # ADO sign-ins, tried in order; the first that works is kept for the rest of the run. Git
    # Credential Manager (when configured for dev.azure.com) holds the operator's own sign-in; the az
    # token is the fallback (for MSA accounts it can be stale after a sign-out, and ADO refuses it).
    tok = sh.run(["az", "account", "get-access-token", "--scope", f"{ADO_RESOURCE}/.default",
                  "--query", "accessToken", "-o", "tsv"], sensitive=True)
    token = tok.stdout.strip() if tok.ok else ""
    signins = []
    helper = sh.run(["git", "-C", repo, "config", "--get-urlmatch", "credential.helper", org_url], timeout=30.0)
    if helper.ok and helper.stdout.strip():
        # Never interactive: a sign-in window nobody answers would hold the push until it times out.
        signins.append(("the git credential helper", {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}))
    if token:
        # The bearer header alone: with X-VSS-ForceMsaPassThrough ADO answers 401 to an MSA account's push.
        signins.append(("the az token", {"GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}))
    if not signins:
        raise DeliveryError("no Azure DevOps sign-in: run `az login` to the ADO tenant, or set up Git Credential "
                            "Manager for dev.azure.com")
    fix = f"sign in once with `git push {repo_url} HEAD:{base}` (Git Credential Manager), or `az login`"

    def git(*args, cwd=repo, timeout=120.0, env=None, meaning=None):
        return sh.run(["git", "-C", str(cwd), *args], timeout=timeout, extra_env=env, meaning=meaning)

    def must(res, what):
        if not res.ok:
            raise DeliveryError(f"{what} failed: {redact.text(res.error_text())}")
        return res

    def remote(what, *args, cwd=repo):
        """A fetch or push to ADO with the first sign-in that works; it is kept for later calls."""
        errors = []
        for name, env in list(signins):
            res = git(*args, cwd=cwd, env=env)
            if res.ok:   # first next time; the others stay (a public repo fetches without any sign-in)
                signins.remove((name, env))
                signins.insert(0, (name, env))
                return res
            # Any failure tries the next sign-in (a helper that cannot prompt, a stale token, a timeout).
            errors.append(f"{name}: {redact.text(res.error_text(200))}")
        raise DeliveryError(f"{what} failed ({'; '.join(errors)}). Fix: {fix}")

    remote("git fetch", "fetch", "--quiet", repo_url, f"+refs/heads/{base}:refs/remotes/delivery/{base}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    must(git("worktree", "add", "--quiet", "-b", branch, str(wt), f"refs/remotes/delivery/{base}"), "git worktree add")
    try:
        drop = gone_targets(c, wt, sh)
        for name in drop:
            log(f"{name} no longer exists (its identity is gone): taking it out of the workspace")
        out = _render(c, wt, drop)
        for w in out["warnings"]:
            log(f"warning: {w}")
        if out["dropped"]:
            must(git("rm", "--quiet", "--cached", "--ignore-unmatch", "--", *out["dropped"], cwd=wt), "git rm")
        must(git("add", "--", *out["files"], cwd=wt), "git add")
        if git("diff", "--cached", "--quiet", cwd=wt, meaning={0: "no changes", 1: "changes found"}).rc == 0:
            log(f"{out['workspace']} already matches {base}: nothing to propose")
            run = queue_onboard(sh, org_url, ado["project"], token, cloud, Path(out["workspace"]).name)
            log(f"queued {onboard_pipeline(cloud)} run {run['run']} to apply and wire it")
            return {"changed": False, "branch": None, "workspace": out["workspace"], "pull_request": None,
                    "pipeline": dict(run, name=onboard_pipeline(cloud))}
        must(git("commit", "--quiet", "-m", f"Onboard {cfg['name']} ({cfg['target']['kind']}) via the console",
                 cwd=wt), "git commit")
        if direct:
            remote(f"git push to {base}", "push", "--quiet", repo_url, f"{branch}:refs/heads/{base}", cwd=wt)
            commit = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            log(f"pushed {commit[:12]} to {base}: {onboard_pipeline(cloud)} applies it")
            return {"changed": True, "branch": base, "workspace": out["workspace"], "commit": commit,
                    "pipeline": {"name": onboard_pipeline(cloud), "url": pipeline_url}, "pull_request": None}
        remote("git push", "push", "--quiet", repo_url, f"{branch}:refs/heads/{branch}", cwd=wt)
        if not token:
            raise DeliveryError("opening a pull request needs an Azure DevOps token: run `az login` to the ADO tenant")
        body = {"sourceRefName": f"refs/heads/{branch}", "targetRefName": f"refs/heads/{base}",
                "title": f"Onboard {cfg['name']} to JFrog OIDC",
                "description": (f"Workspace `{out['workspace']}`, targets: {', '.join(out['targets'])}.\n\n"
                                f"Merging runs {onboard_pipeline(cloud)}: terraform plan and apply, then wire the target.")}
        pr = sh.http("POST", f"{org_url}/{ado['project']}/_apis/git/repositories/{ado['repository']}/pullrequests"
                             "?api-version=7.1",
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                     body=json.dumps(body).encode())
        if pr.status not in (200, 201):
            raise DeliveryError(f"creating the pull request failed: HTTP {pr.status or pr.error}")
        pr_id = pr.json({}).get("pullRequestId")
        url = f"{repo_url}/pullrequest/{pr_id}"
        log(f"opened pull request {url}")
        return {"changed": True, "branch": branch, "workspace": out["workspace"],
                "pull_request": {"id": pr_id, "url": url}}
    finally:
        git("worktree", "remove", "--force", str(wt))
        git("branch", "-D", branch)


def queue_onboard(sh: Shell, org_url: str, project: str, token: str, cloud: str, workspace: str) -> dict:
    """Run <cloud>-workload-onboard for one workspace already on the branch: apply and wire it again."""
    what = f"{cloud}-workload-onboard.yml"
    if not token:
        raise DeliveryError(f"running {what} needs an Azure DevOps token: run `az login` to the ADO tenant")
    # The bearer header alone: with X-VSS-ForceMsaPassThrough ADO refuses a valid token (302/401).
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    api = f"{org_url}/{project}/_apis"
    res = sh.http("GET", f"{api}/build/definitions?includeAllProperties=true&api-version=7.1", headers=headers, timeout=60)
    if not res.ok:
        raise DeliveryError(f"listing the pipelines of {project} failed: HTTP {res.status or res.error}")
    ids = [d.get("id") for d in res.json({}).get("value") or []
           if str((d.get("process") or {}).get("yamlFilename") or "").lstrip("/").split("/")[-1] == what]
    if not ids:
        raise DeliveryError(f"{project} has no pipeline on pipelines/{what}: register it (the pipeline identity stack does), "
                            "then Deliver again")
    res = sh.http("POST", f"{api}/pipelines/{ids[0]}/runs?api-version=7.1", headers=headers, timeout=60,
                  body=json.dumps({"templateParameters": {"workspace": workspace}}).encode())
    if not res.ok:
        raise DeliveryError(f"queuing pipelines/{what} failed: HTTP {res.status or res.error}")
    run_id = res.json({}).get("id")
    return {"run": run_id, "url": f"{org_url}/{project}/_build/results?buildId={run_id}"}


def wire_workspace(ws_dir, sh: Shell, outputs: dict, wire_cls=wiremod.Wire, log=None, env=None) -> list:
    """Wire every config in <ws>/configs. The merged PR is the approval of these diffs."""
    log = log or (lambda _l: None)
    results = []
    for p in sorted((Path(ws_dir) / "configs").glob("*.json")):
        cfg = json.loads(p.read_text())
        w = wire_cls(cfg, sh, outputs)
        try:
            plan = w.plan()
        except wiremod.TargetMissing as exc:   # deleted since onboarding: the others still get wired
            results.append({"name": cfg["name"], "status": "skipped: not found"})
            log(f"{cfg['name']}: skipped, the workload was not found ({exc})")
            continue
        if plan["noop"]:
            results.append({"name": cfg["name"], "status": "already wired"})
            log(f"{cfg['name']}: already wired")
            continue
        if plan.get("deploy_required"):   # App Engine: set by the next build-deploy, not in place
            results.append({"name": cfg["name"], "status": "set at the next build-deploy"})
            log(f"{cfg['name']}: the JFrog variables are set at the next build-deploy (App Engine changes them only by a deploy)")
            continue
        res = w.apply(plan, {"diff_sha256": plan["sha256"]})
        if not res.get("verified"):
            raise DeliveryError(f"{cfg['name']}: the commands ran but the target does not match the desired settings")
        results.append({"name": cfg["name"], "status": "wired"})
        log(f"{cfg['name']}: wired")
    return results


HEALTH_KINDS = {"azure_web_app"}   # targets that serve /healthz/jfrog on a public host name


def _fetch(url: str, timeout: int):
    """GET a health URL: (HTTP status, JSON body or {}). The body carries no token."""
    req = urllib.request.Request(url, headers={"User-Agent": "jfrog-oidc-onboard"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - https URL built from az output
            status, raw = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(65536)
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": str(getattr(e, "reason", e))}
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def check_workspace(ws_dir, sh: Shell, fetch=_fetch, tries: int = 20, wait: int = 30, sleep=time.sleep,
                    log=None, env=None) -> list:
    """Post-deployment check: /healthz/jfrog?fresh=1 (claims, a new exchange and a JFrog read) on
    production and every deployment slot of each web app in the workspace, retried while the app
    restarts after its settings changed. Any slot that never answers 200 fails the run."""
    log = log or (lambda _l: None)
    results, failed = [], []
    for p in sorted((Path(ws_dir) / "configs").glob("*.json")):
        cfg = json.loads(p.read_text())
        if model.get_path(cfg, "target.kind") not in HEALTH_KINDS:
            continue
        a = model.get_path(cfg, "target.azure")
        target = ["-g", a["resource_group"], "-n", a["name"], "--subscription", a["subscription"]]
        site = sh.run(["az", "webapp", "show", *target, "-o", "json"], timeout=120)
        slots = sh.run(["az", "webapp", "deployment", "slot", "list", *target, "-o", "json"], timeout=120)
        if not (site.ok and slots.ok):
            raise DeliveryError(f"{cfg['name']}: could not read its host names")
        hosts = [("production", (site.json() or {}).get("defaultHostName"))]
        hosts += sorted((s.get("name", "").split("/")[-1], s.get("defaultHostName"))
                        for s in slots.json() or [] if isinstance(s, dict))
        for slot, host in hosts:
            if not host:
                continue
            url = f"https://{host}/healthz/jfrog?fresh=1"
            for n in range(1, tries + 1):
                status, body = fetch(url, 60)
                if status == 200:
                    break
                log(f"{cfg['name']} {slot}: HTTP {status} {redact.text(str(body.get('error') or body.get('reason') or ''))[:300]}")
                if n < tries:
                    sleep(wait)
            results.append({"name": cfg["name"], "slot": slot, "status": status})
            log(f"{cfg['name']} {slot}: {'ok' if status == 200 else 'failed'}"
                + (f" as {body['username']}" if status == 200 and body.get("username") else ""))
            if status != 200:
                failed.append(f"{cfg['name']} {slot}")
    if failed:
        raise DeliveryError(f"post-deployment check failed: {', '.join(failed)}")
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m console.deliver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wire", help="wire every target of a workspace after terraform apply")
    w.add_argument("workspace")
    w.add_argument("outputs", help="terraform output -json")
    c = sub.add_parser("check", help="post-deployment check: /healthz/jfrog on every web app and slot of a workspace")
    c.add_argument("workspace")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        def log(line):
            print(redact.text(line), flush=True)
        try:
            res = check_workspace(args.workspace, Shell(mode="live", log=log), log=log, env=os.environ)
        except DeliveryError as exc:
            log(f"##vso[task.logissue type=error]{exc}")
            return 1
        log(json.dumps(res))
        return 0
    raw = json.loads(Path(args.outputs).read_text())
    outputs = {k: v.get("value") if isinstance(v, dict) and "value" in v else v for k, v in raw.items()}

    def say(line):
        print(redact.text(line), flush=True)
    try:
        res = wire_workspace(args.workspace, Shell(mode="live", log=say), outputs, log=say, env=os.environ)
    except (DeliveryError, wiremod.WireError, wiremod.WireRefused) as exc:
        say(f"wire failed: {exc}")
        return 1
    for r in res:
        if r["status"].startswith("skipped"):
            say(f"##vso[task.logissue type=warning]{r['name']}: not wired, the workload was not found")
    say(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
