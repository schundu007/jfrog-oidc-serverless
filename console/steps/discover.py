"""Discover (spec §4.2): look the target up and return its identity facts. Read-only.

Returns {status, kind, corrections[], facts{}, warnings[], errors[], config_patch{}}.

- corrections: the cloud's answer overrides the operator's kind choice
  (an FC1 plan is Flex; a Cloud Run service managed by Cloud Functions is a
  function; a Container App with kind=functionapp is Functions on ACA; a name
  that only exists as the other resource type moves families).
- facts become config.target.discovered; the mapping subject comes from here
  (GCP: the service account's uniqueId; Azure: the managed identity's
  principal/object ID).
- config_patch is a flat {dotted.path: value} map the UI applies to the form.

Nothing here is secret: IDs and resource names only.
"""

from __future__ import annotations

import re
import time

from .. import model
from ..shell import Shell

DEFAULT_COMPUTE_SA = re.compile(r"^\d+-compute@developer\.gserviceaccount\.com$")
APPENGINE_SA = re.compile(r"^[a-z][a-z0-9-]*@appspot\.gserviceaccount\.com$")
NOT_FOUND = re.compile(r"not.?found|could not be found|cannot find|does not exist|NOT_FOUND|ResourceNotFound", re.I)
# gcloud 555: "Cloud Functions API has not been used in project P before or it is disabled"
# az 2.86: "Subscription S is not registered for the Microsoft.App resource provider"
PROVIDER_UNREGISTERED = re.compile(r"not registered for the ([A-Za-z0-9.]+) resource provider", re.I)
API_DISABLED = re.compile(r"SERVICE_DISABLED|has not been used in project|API \[[^\]]+\] not enabled", re.I)
FLEX_SKUS = {"FC1", "FLEXCONSUMPTION"}
CONSUMPTION_SKUS = {"Y1", "DYNAMIC"}


def _dig(d, *path, default=None):
    for p in path:
        if isinstance(d, list):
            d = d[p] if isinstance(p, int) and len(d) > p else None
        elif isinstance(d, dict):
            d = d.get(p)
        else:
            return default
        if d is None:
            return default
    return d


def _prop(d: dict, key: str, default=None):
    """az output is sometimes flattened, sometimes under .properties."""
    if not isinstance(d, dict):
        return default
    if d.get(key) is not None:
        return d[key]
    return _dig(d, "properties", key, default=default)


class Discover:
    def __init__(self, cfg: dict, sh: Shell):
        self.cfg = cfg
        self.sh = sh
        self.kind = model.get_path(cfg, "target.kind")
        self.corrections: list = []
        self.facts: dict = {}
        self.warnings: list = []
        self.errors: list = []
        self.patch: dict = {}

    def warn(self, code, message, fix=""):
        self.warnings.append({"code": code, "message": message, "fix": fix})

    def error(self, code, message, fix=""):
        self.errors.append({"code": code, "message": message, "fix": fix})

    def correct(self, to_kind: str, reason: str):
        if to_kind != self.kind:
            self.corrections.append({"field": "target.kind", "from": self.kind, "to": to_kind, "reason": reason})
            self.kind = to_kind

    # -- entry point ----------------------------------------------------------

    def run(self) -> dict:
        if self.kind not in model.KINDS:
            self.error("unknown_kind", f"unknown target kind {self.kind!r}")
        elif model.cloud_of(self.kind) == "gcp":
            self.gcp()
        else:
            self.azure()
        return self.result()

    def result(self) -> dict:
        if not self.errors:
            self.patch["target.kind"] = self.kind
            self.patch["target.discovered"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                               "kind": self.kind, **self.facts}
        status = "fail" if self.errors else "warn" if (self.warnings or self.corrections) else "ok"
        return {"status": status, "kind": self.kind, "kind_label": model.KINDS.get(self.kind, {}).get("label"),
                "corrections": self.corrections, "facts": self.facts, "warnings": self.warnings,
                "errors": self.errors, "config_patch": self.patch}

    # -- GCP --------------------------------------------------------------------------

    def gcp(self):
        g = model.get_path(self.cfg, "target.gcp") or {}
        project, region, name = g.get("project"), g.get("region"), g.get("name")
        if not (project and region and name):
            return self.error("missing_fields", "project, region and name are required")
        self.facts.update(project=project, region=region, name=name)

        if self.kind == "gcp_run_function":
            fn = self._gcp_function(project, region, name)
            if fn is None:
                svc = self._gcp_service(project, region, name, quiet=True)
                if svc is None:
                    return self.error("not_found", f"no Cloud Run function or service '{name}' in {project}/{region}",
                                      "Check the name, region and project.")
                self.correct("gcp_run_service", "no function by that name, but a Cloud Run service exists")
                return self._gcp_from_service(project, svc)
            return self._gcp_from_function(project, fn)

        if self.kind == "gcp_run_job":
            res = self.sh.run(["gcloud", "run", "jobs", "describe", name, "--region", region, "--project", project,
                               "--format=json"], timeout=90)
            if not res.ok:
                return self._gcp_lookup_failed(res, f"Cloud Run job '{name}'")
            job = res.json() or {}
            spec = _dig(job, "spec", "template", "spec", "template", "spec", default={})
            self.facts["image"] = _dig(spec, "containers", 0, "image")
            return self._gcp_identity(project, spec.get("serviceAccountName"))

        svc = self._gcp_service(project, region, name)
        if svc is None:
            return
        if _dig(svc, "metadata", "labels", "goog-managed-by") == "cloudfunctions":
            self.correct("gcp_run_function", "this Cloud Run service is managed by Cloud Functions (gen2)")
            fn = self._gcp_function(project, region, name)
            if fn:
                return self._gcp_from_function(project, fn)
        return self._gcp_from_service(project, svc)

    def _gcp_service(self, project, region, name, quiet=False):
        res = self.sh.run(["gcloud", "run", "services", "describe", name, "--region", region, "--project", project,
                           "--format=json"], timeout=90)
        if not res.ok:
            if not quiet:
                self._gcp_lookup_failed(res, f"Cloud Run service '{name}'")
            return None
        return res.json() or {}

    def _gcp_function(self, project, region, name):
        """The function, None when it doesn't exist, False when the lookup failed (error recorded)."""
        res = self.sh.run(["gcloud", "functions", "describe", name, "--region", region, "--project", project, "--gen2",
                           "--format=json"], timeout=90)
        if not res.ok:
            if NOT_FOUND.search(res.error_text()):
                return None
            if API_DISABLED.search(res.stderr or ""):
                # No gen2 function can exist while the API is off; the caller
                # falls back to a Cloud Run service by the same name.
                self.warn("functions_api_disabled", f"the Cloud Functions API is disabled in {project}, so '{name}' "
                          "cannot be a Cloud Run function", f"gcloud services enable cloudfunctions.googleapis.com --project {project}")
                return None
            self._gcp_lookup_failed(res, f"Cloud Run function '{name}'")
            return False
        return res.json() or {}

    def _gcp_from_service(self, project, svc):
        spec = _dig(svc, "spec", "template", "spec", default={})
        self.facts["image"] = _dig(spec, "containers", 0, "image")
        self.facts["url"] = _dig(svc, "status", "url")
        return self._gcp_identity(project, spec.get("serviceAccountName"))

    def _gcp_from_function(self, project, fn):
        if fn is False:
            return
        env = fn.get("environment")
        if env and env != "GEN_2":
            return self.error("gen1_function", "this is a 1st-gen Cloud Function; only 2nd gen (Cloud Run) is supported",
                              "Upgrade the function to 2nd gen.")
        self.facts["url"] = _dig(fn, "serviceConfig", "uri") or fn.get("url")
        self.facts["run_service"] = (_dig(fn, "serviceConfig", "service") or "").rsplit("/", 1)[-1] or None
        build_sa = _dig(fn, "buildConfig", "serviceAccount") or ""
        self.facts["build_service_account"] = build_sa.rsplit("/", 1)[-1] or None
        return self._gcp_identity(project, _dig(fn, "serviceConfig", "serviceAccountEmail"))

    def _gcp_identity(self, project, email):
        if not email:
            res = self.sh.run(["gcloud", "projects", "describe", project, "--format=json"], timeout=60)
            number = (res.json() or {}).get("projectNumber") if res.ok else None
            if not number:
                return self.error("default_sa_unresolved", "the target uses the default compute service account, "
                                  "and the project number could not be read", res.error_text(200))
            email = f"{number}-compute@developer.gserviceaccount.com"
            self.facts["service_account_implicit"] = True
        self.facts["service_account"] = email
        if DEFAULT_COMPUTE_SA.match(email) or APPENGINE_SA.match(email):
            self.facts["default_service_account"] = True
            self.warn("default_service_account",
                      f"{email} is a default service account: every workload in the project that runs as it would "
                      "match the JFrog identity mapping",
                      "Create a dedicated service account for this target (the console won't swap it for you), "
                      "redeploy with --service-account, then Discover again.")
        res = self.sh.run(["gcloud", "iam", "service-accounts", "describe", email, "--project", project, "--format=json"],
                          timeout=60)
        if not res.ok:
            return self.error("sa_lookup_failed", f"could not read service account {email}: {res.error_text(200)}",
                              "The mapping needs the SA's uniqueId: you need iam.serviceAccounts.get on it.")
        sa = res.json() or {}
        if sa.get("disabled"):
            self.error("sa_disabled", f"service account {email} is disabled", "Enable it or use another account.")
        self.facts["service_account_unique_id"] = sa.get("uniqueId")
        self.facts["mapping_subject"] = sa.get("uniqueId")

    def _gcp_lookup_failed(self, res, what):
        if NOT_FOUND.search(res.error_text()):
            self.error("not_found", f"{what} not found", "Check the name, region and project.")
        elif "PERMISSION_DENIED" in res.error_text() or "403" in res.error_text():
            self.error("permission_denied", f"no permission to read {what}", res.error_text(200))
        else:
            self.error("lookup_failed", f"could not read {what}", res.error_text(300))

    # -- Azure ------------------------------------------------------------------------

    def azure(self):
        a = model.get_path(self.cfg, "target.azure") or {}
        sub, rg, name = a.get("subscription"), a.get("resource_group"), a.get("name")
        if not (sub and rg and name):
            return self.error("missing_fields", "subscription, resource group and name are required")
        self.facts.update(subscription=sub, resource_group=rg, name=name)
        container_family = self.kind in ("azure_container_app", "azure_container_app_job", "azure_functions_aca")
        first, second = (self._containerapp, self._functionapp) if container_family else (self._functionapp, self._containerapp)
        found = first(sub, rg, name)
        if found is None:
            found = second(sub, rg, name)
            if found is None:
                return self.error("not_found", f"no Container App or Function App '{name}' in resource group {rg}",
                                  "Check the name, resource group and subscription.")
        if found is False:
            return
        resource, data = found
        self.facts["resource_id"] = data.get("id")
        self.facts["location"] = data.get("location")
        self._azure_identity(data, resource)

    def _show(self, what, sub, rg, name):
        res = self.sh.run(["az", *what.split(), "show", "-g", rg, "-n", name, "--subscription", sub, "-o", "json"], timeout=120)
        if res.ok:
            return res.json() or {}
        if NOT_FOUND.search(res.error_text()):
            return None
        m = PROVIDER_UNREGISTERED.search(res.stderr or "")
        if m:
            # Nothing of that type can exist in a subscription that never
            # registered its provider; the caller tries the other family.
            self.warn("provider_not_registered", f"{m.group(1)} is not registered in this subscription, so '{name}' "
                      f"cannot be a {'Function App' if what == 'functionapp' else 'Container App'}",
                      f"az provider register -n {m.group(1)} --subscription {sub} --wait")
            return None
        self.error("lookup_failed", f"az {what} show failed", res.error_text(300))
        return False

    def _containerapp(self, sub, rg, name):
        if self.kind == "azure_container_app_job":
            return self._containerapp_job(sub, rg, name)
        d = self._show("containerapp", sub, rg, name)
        if not d:
            return d
        is_fn = "functionapp" in str(d.get("kind") or "").lower()
        self.correct("azure_functions_aca" if is_fn else "azure_container_app",
                     "Azure reports this Container App as kind=functionapp" if is_fn
                     else "Azure reports this as a plain Container App")
        self.facts["environment_id"] = _prop(d, "managedEnvironmentId") or _prop(d, "environmentId")
        self.facts["image"] = _dig(_prop(d, "template", {}), "containers", 0, "image")
        regs = _dig(_prop(d, "configuration", {}), "registries", default=[]) or []
        self.facts["registries"] = [{"server": r.get("server"), "identity": r.get("identity"),
                                     "username": r.get("username"),
                                     "uses_secret": bool(r.get("passwordSecretRef"))} for r in regs]
        self.patch["target.azure.environment_id"] = self.facts["environment_id"] or ""
        return ("containerapp", d)

    def _containerapp_job(self, sub, rg, name):
        d = self._show("containerapp job", sub, rg, name)
        if not d:
            return d
        self.facts["environment_id"] = _prop(d, "environmentId") or _prop(d, "managedEnvironmentId")
        self.facts["image"] = _dig(_prop(d, "template", {}), "containers", 0, "image")
        self.patch["target.azure.environment_id"] = self.facts["environment_id"] or ""
        return ("containerapp", d)

    def _functionapp(self, sub, rg, name):
        d = self._show("functionapp", sub, rg, name)
        if not d:
            return d
        self.facts["app_kind"] = str(d.get("kind") or "").lower()
        self.facts["key_vault_reference_identity"] = _prop(d, "keyVaultReferenceIdentity")
        env_id = _prop(d, "managedEnvironmentId")
        if env_id:
            self.facts["environment_id"] = env_id
            self.patch["target.azure.environment_id"] = env_id
            self.correct("azure_functions_aca", "this Function App runs on a Container Apps environment")
            return ("functionapp", d)

        tier = sku = ""
        farm = _prop(d, "serverFarmId")
        if farm:
            res = self.sh.run(["az", "functionapp", "plan", "show", "--ids", farm, "-o", "json"], timeout=120)
            plan = (res.json() or {}) if res.ok else {}
            tier = str(_dig(plan, "sku", "tier") or "")
            sku = str(_dig(plan, "sku", "name") or "")
            self.facts.update(plan_id=farm, plan_sku=sku or None, plan_tier=tier or None)
            if not res.ok:
                self.warn("plan_unreadable", "could not read the App Service plan; hosting model guessed from the app",
                          res.error_text(200))
        if sku.upper() in FLEX_SKUS or tier.upper() in FLEX_SKUS or _prop(d, "functionAppConfig") is not None:
            self.correct("azure_functions_flex", f"the plan is Flex Consumption ({sku or tier or 'functionAppConfig'})")
            if model.get_path(self.cfg, "pull_path.enabled"):
                self.patch["pull_path.enabled"] = False
                self.corrections.append({"field": "pull_path.enabled", "from": True, "to": False,
                                         "reason": "Flex Consumption runs code packages and never pulls images"})
            return ("functionapp", d)
        if sku.upper() in CONSUMPTION_SKUS or tier.upper() in CONSUMPTION_SKUS:
            self.error("consumption_plan", "Linux/Windows Consumption (Y1) is not supported",
                       "Move the app to Flex Consumption, Premium or Dedicated.")
            return ("functionapp", d)
        self.correct("azure_functions_premium", f"the plan is {sku or tier or 'Premium/Dedicated'}")
        fx = str(_dig(d, "siteConfig", "linuxFxVersion") or "")
        self.facts["image"] = fx.split("|", 1)[1] if fx.upper().startswith("DOCKER|") else None
        if not self.facts["image"] and model.get_path(self.cfg, "pull_path.enabled"):
            self.warn("code_deployment", "this Function App runs code, not a custom container: there is no image to pull",
                      "Turn the pull path off for this target, or move the app to a custom container.")
        return ("functionapp", d)

    def _azure_identity(self, d: dict, resource: str):
        ident = d.get("identity") or {}
        itype = str(ident.get("type") or "None")
        uas = {k: v for k, v in (ident.get("userAssignedIdentities") or {}).items() if isinstance(v, dict)}
        self.facts["identity_types"] = itype
        self.facts["user_assigned_identities"] = sorted(uas)

        hints = [model.get_path(self.cfg, "target.azure.identity_resource_id") or ""]
        if resource == "functionapp":
            hints.append(self.facts.get("key_vault_reference_identity") or "")
        hints += [r.get("identity") or "" for r in self.facts.get("registries") or []]
        lower = {k.lower(): k for k in uas}
        chosen = next((lower[h.lower()] for h in hints if h and h.lower() in lower), None)
        if chosen is None and len(uas) == 1:
            chosen = next(iter(uas))
        if chosen is None and len(uas) > 1:
            self.error("ambiguous_identity", f"{len(uas)} user-assigned identities and none is referenced by the app",
                       "Set target.azure.identity_resource_id to the identity the app uses for JFrog.")
            return
        if chosen:
            u = uas[chosen]
            self.facts.update(identity_type="user_assigned", identity_resource_id=chosen,
                              principal_id=u.get("principalId"), client_id=u.get("clientId"),
                              mapping_subject=u.get("principalId"))
            self.patch["target.azure.identity_type"] = "user_assigned"
            self.patch["target.azure.identity_resource_id"] = chosen
            return
        if "systemassigned" in itype.replace(" ", "").lower():
            self.facts.update(identity_type="system_assigned", principal_id=ident.get("principalId"),
                              mapping_subject=ident.get("principalId"))
            self.patch["target.azure.identity_type"] = "system_assigned"
            self.warn("system_assigned_identity",
                      "only a system-assigned identity: live probes cannot borrow it, so verification will use the "
                      "/healthz/jfrog self-test endpoint",
                      "Prefer a user-assigned identity: it survives app re-creation, so the JFrog mapping stays valid.")
            return
        self.error("no_identity", "the app has no managed identity",
                   "Assign a user-assigned managed identity to the app, then Discover again.")


def run(cfg: dict, sh: Shell) -> dict:
    return Discover(cfg, sh).run()
