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
from pathlib import Path

from . import model, redact
from .shell import Shell
from .steps import tfgen
from .steps import wire as wiremod

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"   # Azure DevOps
REQUIRED = {"ado": ("organization", "project", "repository")}
STATE_FIELDS = {"gcs": ("bucket",), "azurerm": ("resource_group_name", "storage_account_name", "container_name"),
                "s3": ("bucket", "region"), "kubernetes": ("namespace",)}
STATE_TYPES = {"gcp": "gcs", "azure": "azurerm", "aws": "s3", "openshift": "kubernetes"}
WORKSPACE_FILES = ("backend.tf.json", "console.json", "main.tf.json", "terraform.tfvars.json")


class DeliveryError(RuntimeError):
    pass


def foundation_settings(stacks_dir) -> dict:
    """Each cloud's pipeline identity and state backend, from its foundation stack's outputs
    (terraform/stacks/<cloud>-foundation, local state). A cloud whose foundation isn't deployed
    is left out."""
    def outputs(stack):
        try:
            return {k: v.get("value") for k, v in
                    json.loads((Path(stacks_dir) / stack / "terraform.tfstate").read_text())["outputs"].items()}
        except (OSError, ValueError, KeyError, AttributeError):
            return {}

    found = {"pipeline_identity": {}, "state": {}}
    g = outputs("gcp-foundation")
    if g.get("tfstate_bucket") and (g.get("gcp_service_accounts") or {}).get("setup"):
        found["pipeline_identity"]["gcp"] = g["gcp_service_accounts"]["setup"]
        found["state"]["gcp"] = {"type": "gcs", "bucket": g["tfstate_bucket"]}
    a = outputs("azure-foundation")
    t = a.get("azure_tfstate") or {}
    if a.get("azure_setup_identity_principal_id") and t.get("storage_account"):
        found["pipeline_identity"]["azure"] = a["azure_setup_identity_principal_id"]
        found["state"]["azure"] = {"type": "azurerm", "resource_group_name": t["resource_group"],
                                   "storage_account_name": t["storage_account"], "container_name": t["container"]}
    return found


def load_settings(path, stacks_dir=None) -> dict | None:
    """pipelines/delivery.json (the ADO repository and merge mode), or None when the console
    applies locally. Pipeline identities and state backends come from the foundation stacks."""
    path = Path(path)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    for section, keys in REQUIRED.items():
        missing = [k for k in keys if not (data.get(section) or {}).get(k)]
        if missing:
            raise DeliveryError(f"{path.name}: {section} needs {', '.join(missing)}")
    found = foundation_settings(stacks_dir or path.resolve().parent.parent / "terraform" / "stacks")
    for key in ("pipeline_identity", "state"):
        data[key] = {**(data.get(key) or {}), **found[key]}
    return data


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
    if t == "s3":
        return {"type": t, "bucket": b["bucket"], "region": b["region"], "key": f"workspaces/{ws_name}.tfstate"}
    if t == "kubernetes":
        # Secret tfstate-default-<suffix>: a DNS label, so no underscores.
        return {"type": t, "namespace": b["namespace"], "secret_suffix": ws_name.lower().replace("_", "-")[:50],
                **({"config_context": b["config_context"]} if b.get("config_context") else {})}
    return {"type": t, **{k: b[k] for k in STATE_FIELDS[t]}, "key": f"workspaces/{ws_name}.tfstate"}


def _render(cfg: dict, wt) -> dict:
    """Render into the worktree; returns the workspace path relative to it and its files."""
    rendered = tfgen.render(cfg, Path(wt) / "terraform", modules="../../modules")
    ws = Path(rendered["dir"])
    configs = ws / "configs"
    configs.mkdir(exist_ok=True)
    text = json.dumps(cfg, indent=2, sort_keys=True) + "\n"
    if redact.text(text) != text:
        raise DeliveryError("refusing to commit the config: a value looks like a secret")
    (configs / f"{cfg['name']}.json").write_text(text)
    rel = ws.relative_to(Path(wt)).as_posix()
    files = [f"{rel}/{n}" for n in WORKSPACE_FILES] + [f"{rel}/configs/{cfg['name']}.json"]
    return {"workspace": rel, "files": files, "targets": rendered["targets"], "warnings": rendered["warnings"]}


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
        signins.append(("the git credential helper", {"GIT_TERMINAL_PROMPT": "0"}))
    if token:
        signins.append(("the az token", {"GIT_CONFIG_COUNT": "2",
                        "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                        "GIT_CONFIG_KEY_1": "http.extraheader", "GIT_CONFIG_VALUE_1": "X-VSS-ForceMsaPassThrough: true",
                        "GIT_TERMINAL_PROMPT": "0"}))
    if not signins:
        raise DeliveryError("no Azure DevOps sign-in: run `az login` to the ADO tenant, or set up Git Credential "
                            "Manager for dev.azure.com")
    fix = f"sign in once with `git push {repo_url} HEAD:{base}` (Git Credential Manager), or `az login`"

    def git(*args, cwd=repo, timeout=120.0, env=None):
        return sh.run(["git", "-C", str(cwd), *args], timeout=timeout, extra_env=env)

    def must(res, what):
        if not res.ok:
            raise DeliveryError(f"{what} failed: {redact.text(res.error_text())}")
        return res

    def remote(what, *args, cwd=repo):
        """A fetch or push to ADO with the first sign-in that works; it is kept for later calls."""
        errors = []
        for name, env in list(signins):
            res = git(*args, cwd=cwd, env=env)
            if res.ok:
                signins[:] = [(name, env)]
                return res
            errors.append(f"{name}: {redact.text(res.error_text(200))}")
            if "Authentication failed" not in res.error_text() and "could not read Username" not in res.error_text():
                break
        raise DeliveryError(f"{what} failed ({'; '.join(errors)}). Fix: {fix}")

    remote("git fetch", "fetch", "--quiet", repo_url, f"+refs/heads/{base}:refs/remotes/delivery/{base}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    must(git("worktree", "add", "--quiet", "-b", branch, str(wt), f"refs/remotes/delivery/{base}"), "git worktree add")
    try:
        out = _render(c, wt)
        for w in out["warnings"]:
            log(f"warning: {w}")
        must(git("add", "--", *out["files"], cwd=wt), "git add")
        if git("diff", "--cached", "--quiet", cwd=wt).rc == 0:
            log(f"{out['workspace']} already matches {base}: nothing to propose")
            return {"changed": False, "branch": None, "workspace": out["workspace"], "pull_request": None}
        must(git("commit", "--quiet", "-m", f"Onboard {cfg['name']} ({cfg['target']['kind']}) via the console",
                 cwd=wt), "git commit")
        if direct:
            remote(f"git push to {base}", "push", "--quiet", repo_url, f"{branch}:refs/heads/{base}", cwd=wt)
            commit = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            log(f"pushed {commit[:12]} to {base}: {cloud}-workload-onboard applies it")
            return {"changed": True, "branch": base, "workspace": out["workspace"], "commit": commit,
                    "pipeline": {"name": f"{cloud}-workload-onboard", "url": pipeline_url}, "pull_request": None}
        remote("git push", "push", "--quiet", repo_url, f"{branch}:refs/heads/{branch}", cwd=wt)
        if not token:
            raise DeliveryError("opening a pull request needs an Azure DevOps token: run `az login` to the ADO tenant")
        body = {"sourceRefName": f"refs/heads/{branch}", "targetRefName": f"refs/heads/{base}",
                "title": f"Onboard {cfg['name']} to JFrog OIDC",
                "description": (f"Workspace `{out['workspace']}`, targets: {', '.join(out['targets'])}.\n\n"
                                f"Merging runs {cloud}-workload-onboard: terraform plan and apply, then wire the target.")}
        pr = sh.http("POST", f"{org_url}/{ado['project']}/_apis/git/repositories/{ado['repository']}/pullrequests"
                             "?api-version=7.1",
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                              "X-VSS-ForceMsaPassThrough": "true"},
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


def pipeline_target(cfg: dict, env) -> dict:
    """The pipeline's own login replaces the operator's local one: an AWS profile from the
    console's machine does not exist on the agent (the role comes from AWS_ROLE_ARN), and
    the agent's kubeconfig may name the cluster's context differently (KUBE_CONTEXT)."""
    cfg = copy.deepcopy(cfg)
    t = cfg.get("target") or {}
    if "aws" in t and env.get("AWS_ROLE_ARN"):
        t["aws"]["profile"] = ""
    if "openshift" in t and env.get("KUBE_CONTEXT"):
        t["openshift"]["context"] = env["KUBE_CONTEXT"]
    return cfg


def wire_workspace(ws_dir, sh: Shell, outputs: dict, wire_cls=wiremod.Wire, log=None, env=None) -> list:
    """Wire every config in <ws>/configs. The merged PR is the approval of these diffs."""
    log = log or (lambda _l: None)
    results = []
    for p in sorted((Path(ws_dir) / "configs").glob("*.json")):
        cfg = pipeline_target(json.loads(p.read_text()), env or {})
        w = wire_cls(cfg, sh, outputs)
        plan = w.plan()
        if plan["noop"]:
            results.append({"name": cfg["name"], "status": "already wired"})
            log(f"{cfg['name']}: already wired")
            continue
        res = w.apply(plan, {"diff_sha256": plan["sha256"]})
        if not res.get("verified"):
            raise DeliveryError(f"{cfg['name']}: the commands ran but the target does not match the desired settings")
        results.append({"name": cfg["name"], "status": "wired"})
        log(f"{cfg['name']}: wired")
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m console.deliver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wire", help="wire every target of a workspace after terraform apply")
    w.add_argument("workspace")
    w.add_argument("outputs", help="terraform output -json")
    args = ap.parse_args(argv)
    raw = json.loads(Path(args.outputs).read_text())
    outputs = {k: v.get("value") if isinstance(v, dict) and "value" in v else v for k, v in raw.items()}

    def say(line):
        print(redact.text(line), flush=True)
    try:
        res = wire_workspace(args.workspace, Shell(mode="live", log=say), outputs, log=say, env=os.environ)
    except (DeliveryError, wiremod.WireError, wiremod.WireRefused) as exc:
        say(f"wire failed: {exc}")
        return 1
    say(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
