"""Preflight (spec §4.1): tools, logins, JFrog token, permissions. Read-only.

Every check returns {id, title, status, detail, fix} where status is one of
ok | warn | fail | skip. Permission gaps are reported, never enforced: the
operator may hold rights through groups or PIM that a listing cannot see.

The JFrog token is read from the environment at call time and only ever sent
as an Authorization header; the gcloud access token used for
testIamPermissions lives in a local variable for one request.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

from .. import model
from ..shell import Shell

TF_MIN = (1, 6)
PROD_NAME = re.compile(r"(^|[-_. ])(prod|prd|production|live)([-_. ]|$)", re.I)

# What the generated workspace and the wire step need on the GCP project.
GCP_PERMISSIONS_COMMON = [
    "serviceusage.services.enable",
    "iam.serviceAccounts.create", "iam.serviceAccounts.get", "iam.serviceAccounts.actAs",
    "iam.roles.create",
    "secretmanager.secrets.create", "secretmanager.secrets.setIamPolicy", "secretmanager.versions.add",
    "artifactregistry.repositories.create", "artifactregistry.repositories.update",
    "artifactregistry.repositories.setIamPolicy",
    "run.jobs.create", "run.jobs.setIamPolicy",
    "cloudscheduler.jobs.create",
    "logging.logMetrics.create", "monitoring.alertPolicies.create",
]
GCP_PERMISSIONS_BY_KIND = {
    "gcp_run_service": ["run.services.get", "run.services.update"],
    "gcp_run_job": ["run.jobs.get", "run.jobs.update"],
    "gcp_run_function": ["cloudfunctions.functions.get", "cloudfunctions.functions.update", "run.services.update"],
}
# APIs the target itself needs (Discover and the wire step). The pull-path
# module enables its own APIs (enable_apis = true), so those are not listed.
GCP_APIS_BY_KIND = {
    "gcp_run_service": ["run.googleapis.com"],
    "gcp_run_job": ["run.googleapis.com"],
    "gcp_run_function": ["run.googleapis.com", "cloudfunctions.googleapis.com"],
}
# Resource providers Terraform and the target need. Registering one is a
# subscription-level write, so the console only shows the command.
AZURE_PROVIDERS_COMMON = ["Microsoft.ManagedIdentity"]
AZURE_PROVIDERS_BY_KIND = {
    "azure_container_app": ["Microsoft.App"],
    "azure_container_app_job": ["Microsoft.App"],
    "azure_functions_aca": ["Microsoft.App", "Microsoft.Web"],
    "azure_functions_premium": ["Microsoft.Web"],
    "azure_functions_flex": ["Microsoft.Web"],
}
AZURE_PROVIDERS_PULL = ["Microsoft.App", "Microsoft.KeyVault"]   # rotator job + registry secret
# Role assignments and a custom role are created, so plain Contributor is not enough.
AZURE_WRITE_ROLES = {"Owner"}
AZURE_CONTRIB_ROLES = {"Contributor"}
AZURE_RBAC_ROLES = {"User Access Administrator", "Role Based Access Control Administrator"}
AZURE_KV_ROLES = {"Key Vault Secrets Officer", "Key Vault Administrator"}


def _check(cid, title, status, detail="", fix=""):
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def _get(d, *path):
    for p in path:
        d = d.get(p) if isinstance(d, dict) else None
    return d


def _version_tuple(text: str):
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else None


class Preflight:
    def __init__(self, cfg: dict, sh: Shell, env=None, delivery=False, repo_root=None):
        self.cfg = cfg
        self.repo_root = repo_root
        self.sh = sh
        self.env = os.environ if env is None else env
        self.delivery = delivery
        self.kind = model.get_path(cfg, "target.kind")
        self.cloud = model.cloud_of(self.kind) if self.kind in model.KINDS else None
        self.checks: list = []
        self.facts: dict = {}
        self.config_patch = {}
        self.on_check = None  # callable(check) as each check finishes: the console streams them

    def add(self, *a, **kw):
        self.checks.append(_check(*a, **kw))
        if self.on_check:
            self.on_check(self.checks[-1])

    # -- entry point ----------------------------------------------------------

    def run(self) -> dict:
        if self.cloud is None:
            self.add("config", "Target kind", "fail", f"unknown target kind {self.kind!r}", "Pick a target kind.")
            return self.result()
        # Delivery: the pipeline applies with its own identity; only what Discover needs is checked here.
        if not self.delivery:
            self.terraform()
        if self.cloud == "gcp":
            self.gcp()
        else:
            self.azure()
        self.jfrog()
        self._pipeline_code()
        return self.result()

    def result(self) -> dict:
        counts = {s: sum(c["status"] == s for c in self.checks) for s in ("ok", "warn", "fail", "skip")}
        status = "fail" if counts["fail"] else "warn" if counts["warn"] else "ok"
        return {"status": status, "counts": counts, "checks": self.checks, "facts": self.facts,
                "config_patch": self.config_patch}

    # -- tools ------------------------------------------------------------------

    def terraform(self):
        res = self.sh.run(["terraform", "version", "-json"], timeout=30)
        if res.rc == 127:
            return self.add("tool_terraform", "Terraform installed", "fail", "terraform not found on PATH",
                            "Install Terraform 1.6 or newer: https://developer.hashicorp.com/terraform/install")
        version = (res.json() or {}).get("terraform_version") or res.stdout
        v = _version_tuple(version)
        if not res.ok or not v:
            return self.add("tool_terraform", "Terraform installed", "fail", res.error_text(), "Reinstall Terraform.")
        if v[:2] < TF_MIN:
            return self.add("tool_terraform", "Terraform installed", "fail", f"terraform {version} is older than 1.6",
                            "Upgrade Terraform to 1.6 or newer.")
        self.add("tool_terraform", "Terraform installed", "ok", f"terraform {'.'.join(map(str, v))}")

    # -- GCP --------------------------------------------------------------------------

    def gcp(self):
        project = model.get_path(self.cfg, "target.gcp.project")
        res = self.sh.run(["gcloud", "version", "--format=json"], timeout=60)
        if not res.ok:
            self.add("tool_gcloud", "gcloud installed", "fail",
                     "gcloud not found on PATH" if res.rc == 127 else res.error_text(),
                     "Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install")
            return
        self.add("tool_gcloud", "gcloud installed", "ok", f"Google Cloud SDK {(res.json() or {}).get('Google Cloud SDK', '?')}")

        res = self.sh.run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=json"], timeout=60)
        accounts = [a.get("account") for a in (res.json() or []) if isinstance(a, dict)]
        if not accounts:
            self.add("gcloud_login", "gcloud logged in", "fail", "no active gcloud account", "Run: gcloud auth login")
            return
        self.facts["gcp_account"] = accounts[0]

        # A cheap authenticated call proves the stored credentials still refresh.
        token = self.sh.run(["gcloud", "auth", "print-access-token"], timeout=60, sensitive=True)
        if not token.ok:
            self.add("gcloud_login", "gcloud logged in", "fail",
                     f"{accounts[0]}: {token.error_text(200)}", "Run: gcloud auth login")
            return
        self.add("gcloud_login", "gcloud logged in", "ok", accounts[0])

        if self.delivery:
            del token
            if self._gcp_project(project):
                self._gcp_pipeline_reach(project)
            return
        adc = self.sh.run(["gcloud", "auth", "application-default", "print-access-token"], timeout=60, sensitive=True)
        if adc.ok:
            self.add("gcloud_adc", "Application Default Credentials", "ok", "Terraform's google provider can authenticate")
        else:
            self.add("gcloud_adc", "Application Default Credentials", "fail",
                     "ADC missing or expired; Terraform uses ADC, not your gcloud login",
                     "Run: gcloud auth application-default login")

        if not self._gcp_project(project):
            return

        perms = GCP_PERMISSIONS_COMMON + GCP_PERMISSIONS_BY_KIND.get(self.kind, [])
        resp = self.sh.http(
            "POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{urllib.parse.quote(project)}:testIamPermissions",
            headers={"Authorization": f"Bearer {token.stdout.strip()}", "Content-Type": "application/json"},
            body=json.dumps({"permissions": perms}).encode(), timeout=30)
        del token
        if not resp.ok:
            self.add("gcp_permissions", "IAM permissions on the project", "warn",
                     f"testIamPermissions returned {resp.status or resp.error}", "Could not check; Plan will show any gaps.")
            return
        granted = set((resp.json() or {}).get("permissions", []))
        missing = [p for p in perms if p not in granted]
        if missing:
            self.add("gcp_permissions", "IAM permissions on the project", "warn",
                     f"missing {len(missing)} of {len(perms)}: {', '.join(missing)}",
                     "Ask for roles/owner on a non-prod project, or a role with these permissions. Apply will fail without them.")
        else:
            self.add("gcp_permissions", "IAM permissions on the project", "ok", f"all {len(perms)} permissions granted")

    def _gcp_project(self, project) -> bool:
        res = self.sh.run(["gcloud", "projects", "describe", project, "--format=json"], timeout=60)
        info = res.json() or {}
        if not res.ok:
            self.add("gcp_project", f"Project {project}", "fail", res.error_text(300),
                     "Check the project ID and that your account can see it.")
            return False
        if info.get("lifecycleState") != "ACTIVE":
            self.add("gcp_project", f"Project {project}", "fail", f"project state {info.get('lifecycleState')}", "")
            return False
        self.facts["gcp_project_number"] = info.get("projectNumber")
        detail = f"{info.get('name', project)} (#{info.get('projectNumber')})"
        prodlike = PROD_NAME.search(project) or PROD_NAME.search(str(info.get("name", "")))
        self.add("gcp_project", f"Project {project}", "warn" if prodlike else "ok",
                 detail + (" — name looks like production" if prodlike else ""),
                 "Prove the setup on a non-prod project first." if prodlike else "")
        self._gcp_apis(project)
        return True

    def _pipeline_identity(self, cloud):
        return ((self.delivery if isinstance(self.delivery, dict) else {}).get("pipeline_identity") or {}).get(cloud)

    # The pipeline wires and applies with the code in the ADO repo, not this checkout's.
    PIPELINE_CODE = ("console", "terraform/modules")

    def _pipeline_code(self):
        ado = (self.delivery if isinstance(self.delivery, dict) else {}).get("ado")
        if not ado or not self.repo_root:
            return
        title = "Pipeline runs this console's code"
        branch = ado.get("target_branch") or "main"
        stale = []
        for path in self.PIPELINE_CODE:
            local = self.sh.run(["git", "-C", str(self.repo_root), "rev-parse", f"HEAD:{path}"], timeout=30)
            remote = self.sh.run(["az", "devops", "invoke", "--org", f"https://dev.azure.com/{ado['organization']}",
                                  "--area", "git", "--resource", "items", "--route-parameters",
                                  f"project={ado['project']}", f"repositoryId={ado['repository']}",
                                  "--query-parameters", f"path=/{path}", f"versionDescriptor.version={branch}",
                                  "--api-version", "7.1", "-o", "json"], timeout=60)
            if not local.ok or not remote.ok:
                return self.add("pipeline_code", title, "warn",
                                f"could not compare {path}/: {(remote if local.ok else local).error_text(200)}",
                                f"Check that the ADO repo's {branch} has this checkout's {path}/.")
            if local.stdout.strip() != str((remote.json() or {}).get("objectId", "")):
                stale.append(path)
        if not stale:
            return self.add("pipeline_code", title, "ok", f"{', '.join(self.PIPELINE_CODE)} match {branch}")
        self.add("pipeline_code", title, "fail",
                 f"the ADO repo's {branch} has a different {', '.join(p + '/' for p in stale)}",
                 f"Merge and push this checkout to the ADO repo: git push "
                 f"https://dev.azure.com/{ado['organization']}/{ado['project']}/_git/{ado['repository']} HEAD:{branch}")

    def _gcp_pipeline_reach(self, project):
        """Existing apps can live in any project: the pipeline's setup account needs a role in this one."""
        sa = self._pipeline_identity("gcp")
        if not sa:
            return
        res = self.sh.run(["gcloud", "projects", "get-iam-policy", project, "--format=json"], timeout=60)
        if not res.ok:
            return self.add("pipeline_reach", "Pipeline can change this project", "warn", res.error_text(200),
                            f"Check that {sa} has roles in {project}.")
        member = f"serviceAccount:{sa}"
        roles = sorted({b.get("role") for b in (res.json() or {}).get("bindings") or [] if member in (b.get("members") or [])})
        if roles:
            return self.add("pipeline_reach", "Pipeline can change this project", "ok", ", ".join(roles))
        self.add("pipeline_reach", "Pipeline can change this project", "fail", f"{sa} has no role in {project}",
                 f"Grant it the setup roles in {project} (terraform/stacks/ado-trust), then Preflight again.")

    def _azure_pipeline_reach(self, sub, rg_id):
        principal = self._pipeline_identity("azure")
        if not principal:
            return
        res = self.sh.run(["az", "role", "assignment", "list", "--assignee", principal, "--scope", rg_id,
                           "--include-inherited", "--subscription", sub, "-o", "json"], timeout=60)
        if not res.ok:
            return self.add("pipeline_reach", "Pipeline can change this resource group", "warn", res.error_text(200),
                            "Check the pipeline setup identity's role assignments.")
        roles = sorted({a.get("roleDefinitionName") for a in res.json() or [] if a.get("roleDefinitionName")})
        if {"Owner", "Contributor"} & set(roles):
            return self.add("pipeline_reach", "Pipeline can change this resource group", "ok", ", ".join(roles))
        self.add("pipeline_reach", "Pipeline can change this resource group", "fail",
                 f"the pipeline setup identity has {', '.join(roles) or 'no role'} here",
                 f"Grant it Contributor and RBAC Administrator on subscription {sub} (terraform/stacks/ado-trust).")

    # -- Azure ------------------------------------------------------------------------

    def azure(self):
        sub = model.get_path(self.cfg, "target.azure.subscription")
        rg = model.get_path(self.cfg, "target.azure.resource_group")
        res = self.sh.run(["az", "version", "-o", "json"], timeout=90)
        if not res.ok:
            self.add("tool_az", "Azure CLI installed", "fail",
                     "az not found on PATH" if res.rc == 127 else res.error_text(),
                     "Install the Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli")
            return
        self.add("tool_az", "Azure CLI installed", "ok", f"azure-cli {(res.json() or {}).get('azure-cli', '?')}")

        res = self.sh.run(["az", "account", "show", "--subscription", sub, "-o", "json"], timeout=90)
        acct = res.json() or {}
        if not res.ok:
            hint = "Run: az login" if "az login" in res.error_text() else \
                "Check the subscription ID, and that your az login can see it (az account list)."
            self.add("az_login", "az logged in, subscription visible", "fail", res.error_text(300), hint)
            return
        user = acct.get("user") or {}
        self.facts.update({"azure_subscription_name": acct.get("name"), "azure_tenant": acct.get("tenantId"),
                           "azure_user": user.get("name"), "azure_user_type": user.get("type")})
        self.add("az_login", "az logged in, subscription visible", "ok",
                 f"{user.get('name')} ({user.get('type')}) → {acct.get('name')}")
        if PROD_NAME.search(str(acct.get("name", ""))):
            self.add("azure_subscription_prod", "Subscription is non-prod", "warn",
                     f"subscription '{acct.get('name')}' looks like production",
                     "Prove the setup on a non-prod subscription first.")
        default = self.sh.run(["az", "account", "show", "-o", "json"], timeout=90).json() or {}
        if default.get("id") and default.get("id") != sub:
            self.add("az_default_subscription", "Default subscription", "ok",
                     f"az default is '{default.get('name')}'; every console call passes --subscription {sub}")

        res = self.sh.run(["az", "group", "show", "-n", rg, "--subscription", sub, "-o", "json"], timeout=90)
        if not res.ok:
            self.add("azure_rg", f"Resource group {rg}", "fail", res.error_text(300), "Check the resource group name.")
            return
        rg_id = (res.json() or {}).get("id", f"/subscriptions/{sub}/resourceGroups/{rg}")
        self._azure_providers(sub)
        self.add("azure_rg", f"Resource group {rg}", "ok", (res.json() or {}).get("location", ""))
        if self.delivery:
            self._azure_pipeline_reach(sub, rg_id)
            return

        oid = self._azure_principal_id(user)
        if not oid:
            self.add("azure_permissions", "Role assignments", "warn", "could not resolve your principal's object ID",
                     "Check manually: Owner, or Contributor + User Access Administrator on the resource group.")
            return
        roles = self._roles(oid, rg_id, sub)
        if roles is None:
            self.add("azure_permissions", "Role assignments on the resource group", "warn",
                     "could not list role assignments", "Check manually.")
        elif roles & AZURE_WRITE_ROLES or (roles & AZURE_CONTRIB_ROLES and roles & AZURE_RBAC_ROLES):
            self.add("azure_permissions", "Role assignments on the resource group", "ok", ", ".join(sorted(roles)))
        else:
            self.add("azure_permissions", "Role assignments on the resource group", "warn",
                     f"found: {', '.join(sorted(roles)) or 'none'}",
                     "The workspace creates role assignments and a custom role: needs Owner, or Contributor plus "
                     "User Access Administrator (or RBAC Administrator), on the resource group.")

        kv = model.get_path(self.cfg, "target.azure.key_vault_id")
        if model.get_path(self.cfg, "pull_path.enabled") and kv:
            kv_roles = self._roles(oid, kv, sub)
            if kv_roles is not None and kv_roles & AZURE_KV_ROLES:
                self.add("azure_kv_permissions", "Key Vault data-plane role", "ok", ", ".join(sorted(kv_roles & AZURE_KV_ROLES)))
            else:
                self.add("azure_kv_permissions", "Key Vault data-plane role", "warn",
                         "no Key Vault Secrets Officer/Administrator on the vault (Owner does not grant data-plane access)",
                         "Terraform writes the placeholder secret: grant yourself Key Vault Secrets Officer on the vault.")
        self.add("azure_entra", "Entra app registration rights", "skip",
                 "not checked: the workspace creates an app registration for the audience",
                 "You need Application Developer (or the tenant's 'users can register applications' setting).")

    def _gcp_apis(self, project):
        need = GCP_APIS_BY_KIND.get(self.kind, [])
        res = self.sh.run(["gcloud", "services", "list", "--enabled", "--project", project, "--format=json"], timeout=120)
        if not res.ok:
            self.add("gcp_apis", "Target APIs enabled", "warn", f"could not list enabled APIs: {res.error_text(200)}",
                     "Check manually: " + ", ".join(need))
            return
        enabled = {_get(s, "config", "name") for s in (res.json() or []) if isinstance(s, dict)}
        missing = [a for a in need if a not in enabled]
        if missing:
            self.add("gcp_apis", "Target APIs enabled", "fail", "disabled: " + ", ".join(missing),
                     " && ".join(f"gcloud services enable {a} --project {project}" for a in missing))
        else:
            self.add("gcp_apis", "Target APIs enabled", "ok", ", ".join(need))

    def _azure_providers(self, sub):
        need = list(AZURE_PROVIDERS_COMMON + AZURE_PROVIDERS_BY_KIND.get(self.kind, []))
        if model.get_path(self.cfg, "pull_path.enabled"):
            need += AZURE_PROVIDERS_PULL
        need = sorted(set(need))
        res = self.sh.run(["az", "provider", "list", "--subscription", sub, "--query",
                           "[].{namespace:namespace,state:registrationState}", "-o", "json"], timeout=120)
        if not res.ok:
            self.add("azure_providers", "Resource providers registered", "warn",
                     f"could not list providers: {res.error_text(200)}", "Check manually: " + ", ".join(need))
            return
        state = {str(p.get("namespace", "")).lower(): p.get("state") for p in (res.json() or []) if isinstance(p, dict)}
        missing = [n for n in need if state.get(n.lower()) != "Registered"]
        if missing:
            self.add("azure_providers", "Resource providers registered", "fail",
                     "not registered: " + ", ".join(f"{n} ({state.get(n.lower()) or 'NotRegistered'})" for n in missing),
                     " && ".join(f"az provider register -n {n} --subscription {sub} --wait" for n in missing))
        else:
            self.add("azure_providers", "Resource providers registered", "ok", ", ".join(need))

    def _azure_principal_id(self, user: dict):
        if user.get("type") == "servicePrincipal":
            res = self.sh.run(["az", "ad", "sp", "show", "--id", str(user.get("name")), "--query", "id", "-o", "tsv"],
                              timeout=90)
        else:
            res = self.sh.run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"], timeout=90)
        oid = res.stdout.strip() if res.ok else ""
        return oid if re.fullmatch(r"[0-9a-fA-F-]{36}", oid) else None

    def _roles(self, oid: str, scope: str, sub: str):
        res = self.sh.run(["az", "role", "assignment", "list", "--assignee", oid, "--scope", scope,
                           "--include-inherited", "--include-groups", "--subscription", sub, "-o", "json"], timeout=120)
        if not res.ok:
            return None
        return {a.get("roleDefinitionName") for a in (res.json() or []) if isinstance(a, dict) and a.get("roleDefinitionName")}

    # -- JFrog ------------------------------------------------------------------------

    def _expected_issuer(self):
        """The iss this target's tokens carry, or None when it is not known yet."""
        if self.cloud == "gcp":
            return "https://accounts.google.com"
        tenant = self.facts.get("azure_tenant")
        return f"https://login.microsoftonline.com/{tenant}/v2.0" if tenant else None

    def jfrog(self):
        url = str(model.get_path(self.cfg, "jfrog.url", "")).rstrip("/")
        problem = model.jfrog_origin_error(url)
        if problem:
            return self.add("jfrog_url", "JFrog URL", "fail", problem,
                            "Use the platform origin, e.g. https://acme.jfrog.io; the client adds every path itself.")
        ping = self.sh.http("GET", f"{url}/access/api/v1/system/ping", timeout=15)
        if not ping.ok:
            return self.add("jfrog_reachable", f"JFrog reachable ({url})", "fail",
                            f"ping returned {ping.status or ping.error}",
                            "Check the URL, VPN/proxy, and that the platform is up.")
        self.add("jfrog_reachable", f"JFrog reachable ({url})", "ok", "access ping OK")

        if self.delivery:   # the pipeline gets its JFrog admin token through OIDC
            return
        token = self.env.get("JFROG_ACCESS_TOKEN", "")
        if not token:
            return self.add("jfrog_token", "JFROG_ACCESS_TOKEN", "fail", "not set in the console's environment",
                            "Create an admin-scoped access token in JFrog, export JFROG_ACCESS_TOKEN, and restart the console.")
        auth = {"Authorization": f"Bearer {token}"}
        res = self.sh.http("GET", f"{url}/access/api/v1/oidc", headers=auth, timeout=20)
        if res.status == 401:
            return self.add("jfrog_token", "JFROG_ACCESS_TOKEN", "fail", "rejected (401): expired or wrong platform",
                            "Create a new token on this platform and restart the console.")
        if res.status == 403:
            return self.add("jfrog_token", "JFROG_ACCESS_TOKEN", "fail", "lacks admin rights (403 on OIDC integrations)",
                            "Use a platform admin (or project admin) token.")
        if not res.ok:
            return self.add("jfrog_token", "JFROG_ACCESS_TOKEN", "fail", f"OIDC list returned {res.status or res.error}", "")
        self.add("jfrog_token", "JFROG_ACCESS_TOKEN", "ok", "present, admin ✓")

        listed = {i.get("name"): i for i in (res.json() or []) if isinstance(i, dict) and i.get("name")}
        self.config_patch["jfrog.adopt_integration"] = False
        existing = set(listed)
        name = model.get_path(self.cfg, f"jfrog.integration_names.{self.cloud}")
        self.facts["jfrog_integrations"] = sorted(existing)
        want_issuer = self._expected_issuer()
        have_issuer = str((listed.get(name) or {}).get("issuer_url") or "").rstrip("/")
        if name in existing and want_issuer and have_issuer and have_issuer != want_issuer:
            # The AKS pilot lost hours to an integration name that belonged to another
            # cluster's issuer. A name is never reused across issuers.
            self.add("jfrog_integration", f"OIDC integration '{name}'", "fail",
                     f"already exists for another issuer ({have_issuer}); this target's tokens come from {want_issuer}",
                     "Pick a new integration name. Do not edit or delete the existing one: other workloads trust it.")
        elif name in existing and want_issuer and have_issuer == want_issuer:
            # Same issuer: Terraform adopts it (an import block) instead of failing on a duplicate.
            self.config_patch["jfrog.adopt_integration"] = True
            self.add("jfrog_integration", f"OIDC integration '{name}'", "ok",
                     f"already exists for {have_issuer}; it will be reused")
        elif name in existing:
            self.add("jfrog_integration", f"OIDC integration '{name}'", "warn",
                     "already exists; Terraform will fail to create it unless it is imported or shared",
                     "If another console workspace owns it, reuse that workspace. Otherwise pick a new name, "
                     "or import it in M4.")
        else:
            self.add("jfrog_integration", f"OIDC integration '{name}'", "ok", "will be created")

        group = model.get_path(self.cfg, "jfrog.reader_group")
        res = self.sh.http("GET", f"{url}/access/api/v2/groups/{urllib.parse.quote(str(group), safe='')}", headers=auth,
                           timeout=20)
        if res.ok:
            self.add("jfrog_group", f"Reader group '{group}'", "ok", "exists")
        elif res.status == 404:
            self.add("jfrog_group", f"Reader group '{group}'", "fail", "not found",
                     "Create the group with read permission on the repositories the target uses.")
        else:
            self.add("jfrog_group", f"Reader group '{group}'", "warn", f"lookup returned {res.status or res.error}", "")


def run(cfg: dict, sh: Shell, env=None, delivery=False, repo_root=None, on_check=None) -> dict:
    """delivery: the ADO pipeline applies with its own OIDC JFrog token, so no local one is needed.
    Pass the delivery settings (pipelines/delivery.json) to also check the pipeline's reach, and
    repo_root to check the pipeline runs this checkout's console and modules."""
    pf = Preflight(cfg, sh, env, delivery, repo_root)
    pf.on_check = on_check
    return pf.run()
