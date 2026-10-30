"""Azure DevOps: queue a pipeline run, poll it, fetch its logs (spec §5).

Everything goes through the operator's `az login` (the azure-devops extension):
- queue: az pipelines run --name P --branch B --parameters k=v ... -o json
- poll:  az pipelines runs show --id N -o json
- logs:  ADO REST _apis/build/builds/N/logs with a bearer from
         az account get-access-token --resource <ADO app id>. That call is
         sensitive: its output is never logged or recorded, and the bearer is
         only ever an Authorization header to dev.azure.com.
- the template's JfrogDownloadToken must be a *secret* pipeline variable:
  az pipelines variable list reports isSecret. It is never a parameter.
"""

from __future__ import annotations

import re

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
SECRET_PARAM = re.compile(r"(?i)token|secret|password|api_?key|credential|pat$")


class AdoError(RuntimeError):
    pass


def org_url(org: str) -> str:
    org = (org or "").strip().rstrip("/")
    if not org:
        raise AdoError("ADO organization is not set (Configure → ADO)")
    return org if org.startswith("https://") else f"https://dev.azure.com/{org}"


def param_args(parameters: dict) -> list:
    out = []
    for k, v in parameters.items():
        if not PARAM_NAME.match(k):
            raise AdoError(f"bad parameter name {k!r}")
        if SECRET_PARAM.search(k):
            raise AdoError(f"{k} looks like a secret; secrets are pipeline secret variables, never parameters")
        out.append(f"{k}={str(v).lower() if isinstance(v, bool) else v}")
    return out


class Ado:
    def __init__(self, shell, organization: str, project: str):
        self.sh, self.org, self.project = shell, org_url(organization), project
        if not project:
            raise AdoError("ADO project is not set (Configure → ADO)")

    def _scope(self):
        return ["--org", self.org, "--project", self.project, "-o", "json"]

    def queue(self, pipeline, branch, parameters) -> dict:
        argv = ["az", "pipelines", "run", "--name", pipeline, "--branch", branch]
        params = param_args(parameters)
        if params:
            argv += ["--parameters", *params]
        res = self.sh.run(argv + self._scope(), timeout=120)
        if not res.ok:
            raise AdoError(f"az pipelines run failed: {res.error_text()}")
        data = res.json({}) or {}
        return {"id": data.get("id"), "url": (data.get("_links") or {}).get("web", {}).get("href", ""),
                "status": data.get("status")}

    def show(self, run_id) -> dict:
        res = self.sh.run(["az", "pipelines", "runs", "show", "--id", str(run_id)] + self._scope(), timeout=60)
        if not res.ok:
            raise AdoError(f"az pipelines runs show failed: {res.error_text()}")
        d = res.json({}) or {}
        return {"status": d.get("status"), "result": d.get("result"), "finish": d.get("finishTime")}

    def _bearer(self) -> str:
        res = self.sh.run(["az", "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken",
                           "-o", "tsv"], timeout=60, sensitive=True)
        if not res.ok or not res.stdout.strip():
            raise AdoError("could not get an ADO access token from az (az login?)")
        return res.stdout.strip()

    def logs(self, build_id) -> str:
        """All log files of a build, concatenated in order."""
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        base = f"{self.org}/{self.project}/_apis/build/builds/{int(build_id)}/logs"
        idx = self.sh.http("GET", f"{base}?api-version=7.1", headers=headers, sensitive=False)
        if not idx.ok:
            raise AdoError(f"listing logs failed: HTTP {idx.status} {idx.error}")
        parts = []
        for entry in sorted((idx.json({}) or {}).get("value", []), key=lambda e: e.get("id", 0)):
            r = self.sh.http("GET", f"{base}/{int(entry['id'])}?api-version=7.1", headers=headers)
            if r.ok:
                parts.append(r.body)
        return "\n".join(parts)

    def secret_variable(self, pipeline, name="JfrogDownloadToken") -> dict:
        res = self.sh.run(["az", "pipelines", "variable", "list", "--pipeline-name", pipeline] + self._scope(), timeout=60)
        if not res.ok:
            return {"status": "unknown", "detail": res.error_text()}
        v = (res.json({}) or {}).get(name)
        if v is None:
            return {"status": "missing", "detail": f"{name} is not defined on {pipeline}: add it as a secret variable"}
        if not v.get("isSecret"):
            return {"status": "not-secret", "detail": f"{name} is defined but NOT secret: mark it 'Keep this value secret'"}
        return {"status": "ok", "detail": f"{name} is a secret variable"}
