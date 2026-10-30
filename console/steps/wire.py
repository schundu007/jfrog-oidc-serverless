"""Wire the target (spec §4.4): desired settings vs what the target has, a diff to
approve, and argv-only commands that apply exactly that diff.

plan()  reads the target (read-only), compares only the settings this console
        manages, and returns {diff[], commands[], sha256, noop, notes[], iac{}}.
        sha256 covers the diff (with current values) and the commands, so an
        approval is for one exact change against one observed state.
apply() refuses unless approval.diff_sha256 matches, re-reads the target and
        refuses if anything changed since the plan, runs the commands, then
        reads again and reports whether the target now matches.

Never exposed: app settings other than the managed keys (an App Service
settings list holds storage keys and connection strings, so that read is a
sensitive call and everything else is dropped), and current registry
passwords. Key Vault references are references, not secrets, but redact masks
`keyvaultref:` in logs anyway.
"""

from __future__ import annotations

import hashlib
import json

from .. import model, redact
from ..shell import Shell
from .aws import AwsWire

SECRET_NAME = "jfrog-registry-token"
HIDDEN = "[set, not shown]"
CONTAINER_KINDS = ("azure_container_app", "azure_container_app_job", "azure_functions_aca")


class WireError(RuntimeError):
    pass


class WireRefused(PermissionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _dig(d, *path):
    for p in path:
        if isinstance(d, list):
            d = d[p] if isinstance(p, int) and len(d) > p else None
        elif isinstance(d, dict):
            d = d.get(p)
        else:
            return None
    return d


def _entry(scope, key, current, desired):
    action = "same" if current == desired else "add" if current is None else "change"
    return {"scope": scope, "setting": key, "current": current, "desired": desired, "action": action}


class Wire(AwsWire):
    def __init__(self, cfg: dict, sh: Shell, outputs: dict):
        self.cfg, self.sh, self.out = cfg, sh, outputs or {}
        self.kind = model.get_path(cfg, "target.kind")
        self.cloud = model.cloud_of(self.kind)
        self.d = model.get_path(cfg, "target.discovered") or {}
        self.jfrog_url = str(model.get_path(cfg, "jfrog.url", "")).rstrip("/")
        # Registry server for image pulls: the Docker repo's dedicated host, never the
        # platform host (repository-path pulls failed on the AKS pilot's instance).
        self.docker_host = model.docker_host(cfg)
        self.pull = (bool(model.get_path(cfg, "pull_path.enabled")) and self.kind != "azure_functions_flex"
                     and self.cloud in model.PULL_CLOUDS)
        self._aws_cache = {}
        self.runtime = bool(model.get_path(cfg, "runtime.enabled", True))
        self.dedicated = self._dedicated()
        if self.dedicated and self.cloud == "azure":
            # The app runs as the identity the workspace created: env and Key Vault references use it.
            self.d = {**self.d, "identity_type": "user_assigned", "identity_resource_id": self.dedicated["id"],
                      "client_id": self.dedicated.get("client_id")}

    def _dedicated(self):
        """The identity the workspace created for an existing app without one (None when it has its own)."""
        if not self.d.get("dedicated_identity"):
            return None
        key = {"gcp": "dedicated_service_accounts", "azure": "dedicated_identities", "aws": "dedicated_roles",
               "openshift": "dedicated_service_accounts"}[self.cloud]
        made = (self.out.get(key) or {}).get(self.cfg["name"])
        if not made:
            raise WireError(f"workspace outputs missing ({key}.{self.cfg['name']}): run Plan and Apply first")
        if self.cloud == "aws":
            return {"id": made, "arn": made}
        return {"id": made} if self.cloud in ("gcp", "openshift") else made

    # -- desired -------------------------------------------------------------------

    def _need(self, *keys):
        missing = [k for k in keys if not self.out.get(k)]
        if missing:
            raise WireError(f"workspace outputs missing ({', '.join(missing)}): run Plan and Apply first")
        return [self.out[k] for k in keys]

    def _need_docker_host(self):
        if not self.docker_host:
            raise WireError("no Docker registry host: set jfrog.docker_repo (or jfrog.docker_host) to the repo "
                            "the app pulls from")

    def desired_env(self) -> dict:
        if not self.runtime:
            return {}
        if self.cloud == "aws":
            return self._aws_env()
        if self.cloud == "gcp":
            provider, audience = self._need("jfrog_provider_name", "jfrog_audience")
            return {"JFROG_OIDC_AUDIENCE": audience, "JFROG_OIDC_PROVIDER": provider, "JFROG_OIDC_SOURCE": "gcp",
                    "JFROG_URL": self.jfrog_url}
        provider, audience = self._need("jfrog_provider_name", "audience_uri")
        env = {"JFROG_URL": self.jfrog_url, "JFROG_OIDC_PROVIDER": provider, "JFROG_OIDC_AUDIENCE": audience,
               "JFROG_OIDC_SOURCE": "azure"}
        if self.d.get("identity_type") == "user_assigned" and self.d.get("client_id"):
            env["AZURE_CLIENT_ID"] = self.d["client_id"]
        return dict(sorted(env.items()))

    def _identity_ref(self):
        return self.d.get("identity_resource_id") if self.d.get("identity_type") == "user_assigned" else "system"

    # -- plan --------------------------------------------------------------------------

    def plan(self) -> dict:
        env = self.desired_env()
        if self.cloud == "gcp":
            diff, commands, notes = self._gcp(env)
        elif self.cloud == "aws":
            diff, commands, notes = self._aws(env)
            self._aws_cache = {}          # the reads held the whole environment: drop it
        elif self._is_container_app():
            diff, commands, notes = self._containerapp(env)
        else:
            diff, commands, notes = self._webapp(env)
        target = self._target_id()
        sha = hashlib.sha256(json.dumps({"target": target, "diff": diff, "commands": commands},
                                        sort_keys=True).encode()).hexdigest()
        return {"target": target, "diff": diff, "commands": commands,
                "display_commands": [" ".join(redact.argv(c)) for c in commands],
                "sha256": sha, "noop": not commands, "notes": notes, "iac": self._iac(env)}

    def _target_id(self) -> str:
        if self.cloud == "gcp":
            g = model.get_path(self.cfg, "target.gcp")
            return f"gcp:{g['project']}:{g['region']}:{self._gcp_resource()}/{self._gcp_name()}"
        if self.cloud == "aws":
            return self._aws_target_id()
        return self.d.get("resource_id") or "azure:" + str(model.get_path(self.cfg, "target.azure.name", ""))

    # -- GCP ---------------------------------------------------------------------------------

    def _gcp_name(self):
        name = model.get_path(self.cfg, "target.gcp.name")
        return (self.d.get("run_service") or name) if self.kind == "gcp_run_function" else name

    def _gcp_resource(self):
        return "jobs" if self.kind == "gcp_run_job" else "services"

    def _gcp(self, env):
        g = model.get_path(self.cfg, "target.gcp")
        loc = ["--region", g["region"], "--project", g["project"]]
        res = self.sh.run(["gcloud", "run", self._gcp_resource(), "describe", self._gcp_name(), *loc, "--format=json"],
                          timeout=120)
        if not res.ok:
            raise WireError(f"could not read the target: {res.error_text(300)}")
        data = res.json() or {}
        spec = _dig(data, "spec", "template", "spec", "template", "spec") if self.kind == "gcp_run_job" \
            else _dig(data, "spec", "template", "spec")
        cur = {e.get("name"): e["value"] if "value" in e else "[secret reference]"
               for e in _dig(spec, "containers", 0, "env") or []}
        diff = [_entry("env", k, cur.get(k), v) for k, v in env.items()]
        changed = {d["setting"]: d["desired"] for d in diff if d["action"] != "same"}
        args = []
        if changed:
            delim = next(c for c in "@#|~" if not any(c in k + v for k, v in changed.items()))
            args.append(f"--update-env-vars=^{delim}^" + delim.join(f"{k}={v}" for k, v in sorted(changed.items())))
        if self.dedicated:
            # An empty serviceAccountName is the project's default compute account.
            e = _entry("identity", "service account", spec.get("serviceAccountName") or self.d.get("service_account"),
                       self.dedicated["id"])
            diff.insert(0, e)
            if e["action"] != "same":
                args.append(f"--service-account={self.dedicated['id']}")
        commands = [["gcloud", "run", self._gcp_resource(), "update", self._gcp_name(), *loc, *args, "--quiet"]] if args else []
        return diff, commands, self._gcp_notes()

    def _gcp_notes(self):
        notes = []
        if self.dedicated:
            roles = self.d.get("replaced_service_account_roles") or []
            notes.append(f"The app moves to {self.dedicated['id']}, which has no project roles"
                         + (f" (the old account had {', '.join(roles)})" if roles else "")
                         + ". Grant it what the app needs, and pass --service-account in your next deploy.")
        if self.kind == "gcp_run_function":
            notes.append("The variables are set on the function's Cloud Run service. A later `gcloud functions deploy` "
                         "replaces them unless it passes the same --set-env-vars, so add them to your deploy or IaC.")
        if self.pull:
            for _, url in sorted((self.out.get("repository_urls") or {}).items()):
                if url.startswith("https://") and "-python.pkg.dev" in url:
                    notes.append(f"requirements.txt: start it with `--index-url {url}` to install through JFrog.")
                elif not url.startswith("https://"):
                    notes.append(f"Images through JFrog: {url}/<image>:<tag> (the remote proxies one JFrog Docker "
                                 "repo through its own host, so no repo key in the path). The console does not change "
                                 "the deployed image; switch it in your next deploy.")
        return notes

    # -- Azure Container Apps ------------------------------------------------------------------

    def _is_container_app(self):
        rid = (self.d.get("resource_id") or "").lower()
        return "/microsoft.app/" in rid or (self.kind in CONTAINER_KINDS and "/microsoft.web/" not in rid)

    def _az_target(self):
        a = model.get_path(self.cfg, "target.azure")
        return ["-g", a["resource_group"], "-n", a["name"], "--subscription", a["subscription"]]

    def _ca(self):
        return ["az", "containerapp", "job"] if self.kind == "azure_container_app_job" else ["az", "containerapp"]

    def _containerapp(self, env):
        res = self.sh.run([*self._ca(), "show", *self._az_target(), "-o", "json"], timeout=120)
        if not res.ok:
            raise WireError(f"could not read the target: {res.error_text(300)}")
        shown = res.json() or {}
        props = shown.get("properties") or {}
        container = _dig(props, "template", "containers", 0) or {}
        cur_env = {e.get("name"): e["value"] if "value" in e else f"[secretRef {e.get('secretRef')}]"
                   for e in container.get("env") or []}
        diff = [_entry("env", k, cur_env.get(k), v) for k, v in env.items()]
        commands, notes = self._assign_identity(shown, diff, [*self._ca(), "identity", "assign", *self._az_target(),
                                                               "--user-assigned"]), []
        changed = [f"{d['setting']}={d['desired']}" for d in diff if d["scope"] == "env" and d["action"] != "same"]
        if changed:
            commands.append([*self._ca(), "update", *self._az_target(), "--container-name",
                             container.get("name") or self.cfg["name"], "--set-env-vars", *changed, "-o", "none"])
        if self.pull:
            url, user = self._need("registry_secret_versionless_url", "registry_username")
            self._need_docker_host()
            ident = self._identity_ref()
            s = {x.get("name"): x for x in _dig(props, "configuration", "secrets") or []}.get(SECRET_NAME)
            cur = None if s is None else (f"Key Vault {s['keyVaultUrl']} via {s.get('identity')}" if s.get("keyVaultUrl") else HIDDEN)
            e = _entry("secret", SECRET_NAME, cur, f"Key Vault {url} via {ident}")
            diff.append(e)
            if e["action"] != "same":
                commands.append([*self._ca(), "secret", "set", *self._az_target(), "--secrets",
                                 f"{SECRET_NAME}=keyvaultref:{url},identityref:{ident}", "-o", "none"])
            r = {x.get("server"): x for x in _dig(props, "configuration", "registries") or []}.get(self.docker_host)
            cur = None if r is None else (f"{r.get('username')} / secret {r.get('passwordSecretRef')}"
                                          if r.get("passwordSecretRef") else f"identity {r.get('identity')}")
            e = _entry("registry", self.docker_host, cur, f"{user} / secret {SECRET_NAME}")
            diff.append(e)
            if e["action"] != "same":
                commands.append([*self._ca(), "registry", "set", *self._az_target(), "--server", self.docker_host,
                                 "--username", user, "--password-secret-name", SECRET_NAME, "-o", "none"])
            image = container.get("image") or ""
            if image and not image.startswith(self.docker_host + "/"):
                notes.append(f"The app runs {image}, not an image from {self.docker_host}; the registry credential "
                             "takes effect once your next deploy points the image at JFrog.")
        return diff, commands, notes

    # -- Azure App Service (Premium / Flex / legacy Functions on ACA) -----------------------------

    def _webapp(self, env):
        res = self.sh.run(["az", "functionapp", "config", "appsettings", "list", *self._az_target(), "-o", "json"],
                          timeout=120, sensitive=True)
        if not res.ok:
            raise WireError(f"could not read the app settings: {res.error_text(300)}")
        wanted = set(env) | {"DOCKER_REGISTRY_SERVER_URL", "DOCKER_REGISTRY_SERVER_USERNAME", "DOCKER_REGISTRY_SERVER_PASSWORD"}
        cur = {}
        for s in res.json() or []:          # keep only the managed keys; everything else is dropped unread
            if isinstance(s, dict) and s.get("name") in wanted:
                v = s.get("value")
                if s["name"] == "DOCKER_REGISTRY_SERVER_PASSWORD" and v and not str(v).startswith("@Microsoft.KeyVault("):
                    v = HIDDEN
                cur[s["name"]] = v
        app = self.sh.run(["az", "functionapp", "show", *self._az_target(), "-o", "json"], timeout=120)
        if not app.ok:
            raise WireError(f"could not read the target: {app.error_text(300)}")
        app = app.json() or {}

        diff = [_entry("env", k, cur.get(k), v) for k, v in env.items()]
        settings = [f"{d['setting']}={d['desired']}" for d in diff if d["action"] != "same"]
        first = self._assign_identity(app, diff, ["az", "functionapp", "identity", "assign", *self._az_target(), "--identities"])
        notes = []
        if self.pull and self.kind == "azure_functions_premium":
            url, user = self._need("registry_secret_versionless_url", "registry_username")
            self._need_docker_host()
            for k, v in (("DOCKER_REGISTRY_SERVER_URL", f"https://{self.docker_host}"),
                         ("DOCKER_REGISTRY_SERVER_USERNAME", user),
                         ("DOCKER_REGISTRY_SERVER_PASSWORD", f"@Microsoft.KeyVault(SecretUri={url})")):
                e = _entry("registry", k, cur.get(k), v)
                diff.append(e)
                if e["action"] != "same":
                    settings.append(f"{k}={v}")
            want = self.d.get("identity_resource_id") if self.d.get("identity_type") == "user_assigned" else "SystemAssigned"
            e = _entry("setting", "keyVaultReferenceIdentity", app.get("keyVaultReferenceIdentity"), want)
            diff.append(e)
            if e["action"] != "same":
                # The identity has to be in place before App Service resolves the new reference.
                first.append(["az", "functionapp", "update", *self._az_target(), "--set",
                              f"keyVaultReferenceIdentity={want}", "-o", "none"])
            fx = str(_dig(app, "siteConfig", "linuxFxVersion") or "")
            image = fx.split("|", 1)[1] if "|" in fx else ""
            if image and not image.startswith(self.docker_host + "/"):
                notes.append(f"The app runs {image}, not an image from {self.docker_host}; point the image at JFrog "
                             "in your next deploy.")
        commands = first
        if settings:
            commands = first + [["az", "functionapp", "config", "appsettings", "set", *self._az_target(),
                                 "--settings", *settings, "-o", "none"]]
        return diff, commands, notes

    def _assign_identity(self, shown: dict, diff: list, assign: list) -> list:
        """Attach the dedicated identity (additive: identities the app already has stay)."""
        if not self.dedicated:
            return []
        rid = self.dedicated["id"]
        have = {k.lower(): v for k, v in (_dig(shown, "identity", "userAssignedIdentities") or {}).items()}
        attached = have.get(rid.lower())
        want = self.dedicated.get("client_id")
        if attached is not None and want and isinstance(attached, dict) and attached.get("clientId") not in (None, want):
            # Re-created under the same resource ID: the app still holds the deleted identity, so
            # detach and attach again to pick up the new one.
            diff.insert(0, _entry("identity", "user-assigned identity", f"{rid} (client {attached['clientId']}, deleted)", rid))
            remove = [a if a != "assign" else "remove" for a in assign]
            return [[*remove, rid, "-o", "none"], [*assign, rid, "-o", "none"]]
        e = _entry("identity", "user-assigned identity", rid if attached is not None else None, rid)
        diff.insert(0, e)
        return [] if e["action"] == "same" else [[*assign, rid, "-o", "none"]]

    # -- IaC ------------------------------------------------------------------------------------

    def _iac(self, env) -> dict:
        iac = {"env": env, "note": "Add these to the target's own IaC or deploy pipeline so the next deploy keeps them."}
        url, user = self.out.get("registry_secret_versionless_url"), self.out.get("registry_username")
        if self.cloud == "azure" and self.pull and url:
            if self._is_container_app():
                iac["azurerm_container_app"] = {
                    "secret": [{"name": SECRET_NAME, "key_vault_secret_id": url, "identity": self._identity_ref()}],
                    "registry": [{"server": self.docker_host, "username": user, "password_secret_name": SECRET_NAME}]}
            elif self.kind == "azure_functions_premium":
                iac["app_settings"] = {"DOCKER_REGISTRY_SERVER_URL": f"https://{self.docker_host}",
                                       "DOCKER_REGISTRY_SERVER_USERNAME": user,
                                       "DOCKER_REGISTRY_SERVER_PASSWORD": f"@Microsoft.KeyVault(SecretUri={url})"}
        return iac

    # -- apply ------------------------------------------------------------------------------------

    def _resolve(self, argv: list) -> list:
        """Commands that send a whole configuration back carry a placeholder; fill it from a fresh read."""
        return self._aws_resolve(argv) if self.cloud == "aws" else argv

    def apply(self, plan: dict, approval) -> dict:
        if not isinstance(approval, dict) or not approval.get("diff_sha256"):
            raise WireRefused("not_approved", "wiring needs the operator's approval of the shown diff")
        if approval["diff_sha256"] != plan.get("sha256"):
            raise WireRefused("diff_hash_mismatch", "the approval is for a different diff; review the current one")
        fresh = self.plan()
        if fresh["sha256"] != plan["sha256"]:
            raise WireRefused("target_changed", "the target changed since the diff was shown; review the new diff")
        ran = []
        for argv in fresh["commands"]:
            # The filled argv may carry the app's other settings: log the placeholder form only.
            res = self.sh.run(self._resolve(argv), timeout=600, sensitive=True, shown=argv)
            self._aws_cache = {}
            if not res.ok:
                raise WireError(f"{' '.join(argv[:4])} failed after {len(ran)} of {len(fresh['commands'])} "
                                f"command(s): {res.error_text(400)}")
            ran.append(" ".join(redact.argv(argv)))
        after = self.plan()
        return {"applied": True, "ran": ran, "verified": after["noop"], "after": after["diff"]}
