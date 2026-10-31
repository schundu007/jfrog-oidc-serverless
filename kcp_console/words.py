"""Log lines in words. The log tells the operator what happened ("Read Google Cloud project
unioxe: ok, 0.5s"), never the command or URL that did it. Values that could be secret never
reach a line: only resource names (a positional argument that is not a flag) are kept, and the
caller still routes the line through redact.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

TOOLS = {"gcloud": "Google Cloud", "az": "Azure", "aws": "AWS", "kubectl": "Kubernetes", "oc": "OpenShift",
         "helm": "Helm", "terraform": "Terraform", "git": "Git", "docker": "Docker", "gsutil": "Cloud Storage",
         "jf": "JFrog CLI", "curl": "HTTP"}

VERBS = {"describe": "Read", "show": "Read", "get": "Read", "get-iam-policy": "Read the access policy of",
         "list": "Listed", "ls": "Listed", "list-revisions": "Listed revisions of", "update": "Updated",
         "set": "Set", "patch": "Updated", "apply": "Applied", "create": "Created", "delete": "Deleted",
         "remove": "Removed", "deploy": "Deployed", "execute": "Ran", "start": "Started", "stop": "Stopped",
         "assign": "Assigned", "add": "Added", "enable": "Enabled", "disable": "Disabled", "wait": "Waited for",
         "rollout": "Rolled out", "replace": "Replaced", "register-task-definition": "Registered a task definition for",
         "update-service": "Updated", "update-function-configuration": "Updated the configuration of",
         "add-iam-policy-binding": "Granted access on", "remove-iam-policy-binding": "Removed access on",
         "version": "Checked the version of", "upgrade": "Upgraded", "install": "Installed", "uninstall": "Uninstalled"}

NOUNS = {("projects",): "project", ("services",): "enabled APIs", ("run", "services"): "Cloud Run service",
         ("run", "jobs"): "Cloud Run job", ("run", "jobs", "executions"): "Cloud Run job run",
         ("functions",): "Cloud Run function", ("iam", "service-accounts"): "service account",
         ("iam", "workload-identity-pools"): "workload identity pool", ("artifacts", "repositories"): "Artifact Registry repository",
         ("secrets",): "secret", ("secrets", "versions"): "secret version", ("auth",): "sign-in",
         ("containerapp",): "container app", ("containerapp", "job"): "container app job",
         ("containerapp", "secret"): "container app secret", ("containerapp", "registry"): "container app registry",
         ("containerapp", "identity"): "container app identity", ("containerapp", "job", "identity"): "container app job identity",
         ("functionapp",): "function app", ("functionapp", "identity"): "function app identity",
         ("functionapp", "config", "appsettings"): "function app settings", ("keyvault", "secret"): "Key Vault secret",
         ("identity",): "managed identity", ("account",): "account", ("group",): "resource group",
         ("acr",): "container registry", ("aks",): "AKS cluster", ("role", "assignment"): "role assignment",
         ("ad", "app"): "Entra app", ("ad", "sp"): "service principal", ("lambda",): "Lambda function",
         ("ecs",): "ECS", ("apprunner",): "App Runner service", ("sts",): "AWS identity", ("iam",): "IAM"}

BOOL_FLAGS = {"--quiet", "-q", "--enabled", "--wait", "--gen2", "--yes", "-y", "--no-wait", "--only-show-errors", "--all",
              "--force", "--async", "--overwrite", "--allow-unauthenticated", "--no-allow-unauthenticated"}


def _positionals(args: list) -> list:
    out, skip = [], False
    for a in args:
        if skip and not a.startswith("-"):
            skip = False
            continue
        skip = False
        if a.startswith("-"):
            skip = "=" not in a and a not in BOOL_FLAGS
            continue
        out.append(a)
    return out


def _result(rc: int, seconds: float) -> str:
    return f"{'ok' if rc == 0 else f'failed (exit {rc})'}, {seconds:.1f}s"


def command(argv: list, rc: int, seconds: float) -> str:
    """One plain-language line for a finished command."""
    tool = Path(argv[0]).name
    name = TOOLS.get(tool, tool)
    words = _positionals(argv[1:])
    return f"{_what(tool, name, words, argv[1:])}: {_result(rc, seconds)}"


def _what(tool, name, words, args) -> str:
    if tool == "terraform":
        sub = words[0] if words else ""
        return {"init": "Prepared Terraform", "plan": "Planned the Terraform changes", "apply": "Applied the Terraform plan",
                "output": "Read the Terraform outputs", "show": "Read the Terraform plan", "validate": "Validated the Terraform code",
                "version": "Checked the Terraform version", "state": "Read the Terraform state",
                "import": "Imported a resource into Terraform state", "destroy": "Destroyed the Terraform resources",
                "fmt": "Formatted the Terraform code", "providers": "Read the Terraform providers"}.get(sub, "Ran Terraform")
    if tool == "git":
        sub = words[0] if words else ""
        if sub == "ls-tree":
            return f"Read this checkout's {words[-1].rstrip('/')}/" if len(words) > 2 else "Read this checkout's files"
        return {"rev-parse": "Read the current commit", "status": "Read the working tree", "diff": "Compared the files",
                "push": "Pushed to the repository", "commit": "Committed the change", "fetch": "Fetched the repository",
                "clone": "Cloned the repository", "checkout": "Switched branch", "add": "Staged the files"}.get(sub, "Ran Git")
    if tool == "az" and words[:1] == ["rest"]:
        url = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--url", "--uri", "-u")), "")
        return f"Called {_service(url)}"
    if tool == "gcloud" and words[:2] == ["auth", "print-access-token"]:
        return "Got a Google Cloud access token"
    if tool == "gcloud" and words[:1] == ["auth"]:
        return "Read the Google Cloud sign-in"
    if tool == "az" and words[:2] == ["account", "get-access-token"]:
        return "Got an Azure access token"
    if tool in ("kubectl", "oc"):
        verb = words[0] if words else ""
        target = " ".join(words[1:3])
        return f"{VERBS.get(verb, verb.capitalize() or 'Ran')} {name} {target}".rstrip()
    if tool == "helm":
        verb = words[0] if words else ""
        return f"{VERBS.get(verb, verb.capitalize() or 'Ran')} Helm release {' '.join(words[1:2])}".rstrip()
    # gcloud / az / aws: <groups...> <verb> [name]
    for i, w in enumerate(words):
        if w in VERBS:
            groups, rest = tuple(words[:i]), words[i + 1:i + 2]
            if w == "version" and not groups:
                return f"Checked the {name} CLI"
            noun = NOUNS.get(groups) or " ".join(groups) or name
            subject = f"{name} {noun}" if noun != name and not noun.startswith(("Cloud Run", "Azure", "AWS")) else noun
            return f"{VERBS[w]} {subject}{' ' + rest[0] if rest else ''}"
    return f"Ran {name} {' '.join(words[:2])}".rstrip()


def _service(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if host.endswith("dev.azure.com") or host.endswith("visualstudio.com"):
        return "Azure DevOps"
    if host == "management.azure.com":
        return "Azure Resource Manager"
    if host == "graph.microsoft.com":
        return "Microsoft Graph"
    if host.endswith("googleapis.com"):
        return f"the Google {host.split('.')[0]} API"
    if host.endswith("jfrog.io") or "jfrog" in host:
        return "JFrog"
    return host or "a web API"


def http(method: str, url: str, status, seconds: float) -> str:
    """One plain-language line for a finished HTTP call: the service and the outcome, never the URL."""
    ok = isinstance(status, int) and 200 <= status < 400
    action = {"GET": "Read from", "HEAD": "Checked", "POST": "Sent a request to", "PUT": "Wrote to",
              "PATCH": "Updated", "DELETE": "Deleted from"}.get(method.upper(), "Called")
    outcome = "ok" if ok else (f"failed (HTTP {status})" if isinstance(status, int) else f"failed ({status})")
    return f"{action} {_service(url)}: {outcome}, {seconds:.1f}s"
