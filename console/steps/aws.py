"""AWS targets: Lambda functions, ECS (Fargate) tasks/services, App Runner services.

Mixed into the Preflight, Discover and Wire classes, so the other clouds' code
is untouched. Identity is the workload's IAM role (Lambda execution role, ECS
task role, App Runner instance role). The token comes from STS
GetWebIdentityToken (IAM outbound identity federation): iss is the account's
issuer, sub the role ARN, aud the JFrog integration's audience.

Every call is `aws ... --region <r> [--profile <p>] --output json` through the
console's Shell. Reads of a workload's configuration are sensitive calls: the
environment of a Lambda, a task definition or an App Runner service may hold
secrets, so only the keys this console manages are kept; the rest are counted,
never shown, logged or hashed into a plan. Commands that must send the whole
environment back (each of these APIs replaces it) carry a placeholder that
apply() fills from a fresh sensitive read.
"""

from __future__ import annotations

import json
import re

from .. import model

AUDIENCE = "jfrog-aws"
ENABLE_FIX = "aws iam enable-outbound-web-identity-federation"
NOT_FOUND = re.compile(r"ResourceNotFound|NotFoundException|ServiceNotFound|not found|does not exist|"
                       r"Unable to describe task definition|MISSING", re.I)
EXPIRED = re.compile(r"ExpiredToken|Token has expired|sso.*expired|Unable to locate credentials|"
                     r"InvalidClientTokenId|could not be found.*profile|The config profile", re.I)
TRUST_PRINCIPAL = {"aws_lambda": "lambda.amazonaws.com", "aws_ecs_task": "ecs-tasks.amazonaws.com",
                   "aws_app_runner": "tasks.apprunner.amazonaws.com"}
# Roles that serve many workloads by design: a mapping on one would trust all of them.
SHARED_ROLE_NAMES = re.compile(r"^(ecsTaskExecutionRole|AWSServiceRoleFor.*|service-role/.*|.*-shared-.*)$", re.I)

PERMISSIONS_COMMON = ["iam:CreateRole", "iam:GetRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:PassRole",
                      "iam:GetOutboundWebIdentityFederationInfo"]
PERMISSIONS_BY_KIND = {
    "aws_lambda": ["lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration"],
    "aws_ecs_task": ["ecs:DescribeServices", "ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition",
                     "ecs:UpdateService"],
    "aws_app_runner": ["apprunner:ListServices", "apprunner:DescribeService", "apprunner:UpdateService"],
}
# Read-only fields of describe-task-definition that register-task-definition refuses.
TASKDEF_READONLY = ("taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
                    "registeredAt", "registeredBy", "deregisteredAt")
RESOLVE = "@resolve:"          # argv placeholder, filled by apply() from a fresh read


def opts(cfg: dict) -> list:
    a = model.get_path(cfg, "target.aws") or {}
    out = ["--region", str(a.get("region") or "")]
    if a.get("profile"):
        out += ["--profile", str(a["profile"])]
    return out + ["--output", "json"]


def role_name(arn: str) -> str:
    return str(arn or "").rsplit("/", 1)[-1]


def caller_role_arn(arn: str) -> str:
    """arn:aws:sts::A:assumed-role/R/session -> arn:aws:iam::A:role/R (paths are not in the STS ARN)."""
    m = re.match(r"^arn:aws[\w-]*:sts::(\d{12}):assumed-role/([^/]+)/", arn or "")
    return f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}" if m else arn


def _json(s: str) -> str:
    return json.dumps(s, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------- Preflight


class AwsPreflight:
    def aws(self):
        a = model.get_path(self.cfg, "target.aws") or {}
        account, region = a.get("account"), a.get("region")
        o = opts(self.cfg)
        res = self.sh.run(["aws", "--version"], timeout=60)
        if not res.ok:
            return self.add("tool_aws", "AWS CLI installed", "fail",
                            "aws not found on PATH" if res.rc == 127 else res.error_text(),
                            "Install AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html")
        self.add("tool_aws", "AWS CLI installed", "ok", (res.stdout or res.stderr).strip().split(" ", 1)[0])

        login = f"aws sso login --profile {a['profile']}" if a.get("profile") else "aws sso login (or aws configure)"
        res = self.sh.run(["aws", "sts", "get-caller-identity", *o], timeout=60)
        who = (res.json() or {}) if res.ok else {}
        if not who.get("Account"):
            return self.add("aws_login", "aws logged in", "fail", res.error_text(300) or "no caller identity",
                            f"Run: {login}")
        self.facts.update(aws_account=who["Account"], aws_caller=who.get("Arn"))
        if who["Account"] != account:
            return self.add("aws_login", "aws logged in", "fail",
                            f"the credentials are for account {who['Account']}, not {account}",
                            "Pick the profile for this account (target.aws.profile) or fix the account ID.")
        self.add("aws_login", "aws logged in", "ok", f"{who.get('Arn')} in {region}")

        res = self.sh.run(["aws", "iam", "list-account-aliases", *o], timeout=60)
        aliases = (res.json() or {}).get("AccountAliases") or [] if res.ok else []
        if any(PROD_NAME.search(x) for x in aliases):
            self.add("aws_account_prod", "Account is non-prod", "warn", f"account alias {', '.join(aliases)} looks like production",
                     "Prove the setup on a non-prod account first.")

        self._aws_federation(o)
        if self.delivery:
            return
        self._aws_permissions(o, who.get("Arn", ""))

    def _aws_federation(self, o):
        title = "IAM outbound identity federation"
        res = self.sh.run(["aws", "iam", "get-outbound-web-identity-federation-info", *o], timeout=60)
        info = (res.json() or {}) if res.ok else {}
        fix = " ".join([ENABLE_FIX, *o[:-2]])
        if not res.ok and re.search(r"AccessDenied|not authorized", res.error_text(), re.I):
            return self.add("aws_federation", title, "fail", "no permission to read the federation status",
                            "Grant iam:GetOutboundWebIdentityFederationInfo, then Preflight again.")
        if not info.get("JwtVendingEnabled") or not info.get("IssuerIdentifier"):
            return self.add("aws_federation", title, "fail",
                            "not enabled in this account: STS cannot issue the JWT JFrog trusts"
                            + (f" ({res.error_text(160)})" if not res.ok else ""),
                            f"Run once per account: {fix}")
        self.facts["aws_issuer"] = info["IssuerIdentifier"].rstrip("/")
        self.add("aws_federation", title, "ok", f"issuer {self.facts['aws_issuer']}")
        # JFrog fetches the issuer's JWKS itself: it has to be public.
        url = f"{self.facts['aws_issuer']}/.well-known/openid-configuration"
        r = self.sh.http("GET", url, timeout=15)
        if not r.ok or not (r.json() or {}).get("jwks_uri"):
            self.add("aws_issuer_public", "Issuer discovery is public", "warn",
                     f"{url} returned {r.status or r.error}", "JFrog must reach the issuer's JWKS; check egress/proxy.")

    def _aws_permissions(self, o, caller):
        actions = PERMISSIONS_COMMON + PERMISSIONS_BY_KIND.get(self.kind, [])
        res = self.sh.run(["aws", "iam", "simulate-principal-policy", "--policy-source-arn", caller_role_arn(caller),
                           "--action-names", *actions, *o], timeout=120)
        if not res.ok:
            return self.add("aws_permissions", "IAM permissions", "warn",
                            f"could not simulate: {res.error_text(160)}", "Plan and Wire will show any gaps.")
        denied = sorted({r.get("EvalActionName") for r in (res.json() or {}).get("EvaluationResults") or []
                         if r.get("EvalDecision") != "allowed"})
        if denied:
            return self.add("aws_permissions", "IAM permissions", "warn", f"denied {len(denied)} of {len(actions)}: {', '.join(denied)}",
                            "Use a role with these actions (AdministratorAccess on a test account). Apply/Wire fail without them.")
        self.add("aws_permissions", "IAM permissions", "ok", f"all {len(actions)} actions allowed")


PROD_NAME = re.compile(r"(^|[-_. ])(prod|prd|production|live)([-_. ]|$)", re.I)


# -------------------------------------------------------------------- Discover


class AwsDiscover:
    def aws(self):
        a = model.get_path(self.cfg, "target.aws") or {}
        account, region, name = a.get("account"), a.get("region"), a.get("name")
        if not (account and region and name):
            return self.error("missing_fields", "account, region and name are required")
        self.facts.update(account=account, region=region, name=name)
        o = opts(self.cfg)
        if not self._aws_issuer(o):
            return
        role = {"aws_lambda": self._lambda, "aws_ecs_task": self._ecs, "aws_app_runner": self._apprunner}[self.kind](o, a)
        if role is False:
            return
        self._aws_identity(o, role)
        if model.get_path(self.cfg, "jfrog.integration_names.aws") == model.DEFAULT_INTEGRATIONS["aws"]:
            # One integration per account issuer: name it after the account.
            self.patch["jfrog.integration_names.aws"] = f"aws-{account}"

    def _aws_failed(self, res, what):
        text = res.error_text(300)
        if EXPIRED.search(text):
            self.error("aws_login", f"the AWS login cannot read {what}: {res.error_text(160)}", "Run: aws sso login")
        elif NOT_FOUND.search(text):
            self.error("not_found", f"{what} not found", "Check the name, region and account.")
        elif re.search(r"AccessDenied|not authorized", text, re.I):
            self.error("permission_denied", f"no permission to read {what}", text)
        else:
            self.error("lookup_failed", f"could not read {what}", text)
        return False

    def _aws_issuer(self, o) -> bool:
        res = self.sh.run(["aws", "iam", "get-outbound-web-identity-federation-info", *o], timeout=60)
        info = (res.json() or {}) if res.ok else {}
        if not info.get("JwtVendingEnabled") or not info.get("IssuerIdentifier"):
            self.error("federation_disabled", "IAM outbound identity federation is not enabled in this account",
                       f"Run once per account: {' '.join([ENABLE_FIX, *o[:-2]])}")
            return False
        self.facts["issuer"] = info["IssuerIdentifier"].rstrip("/")
        return True

    def _lambda(self, o, a):
        res = self.sh.run(["aws", "lambda", "get-function-configuration", "--function-name", a["name"], *o],
                          timeout=90, sensitive=True)          # Environment may hold secrets
        if not res.ok:
            return self._aws_failed(res, f"Lambda function '{a['name']}'")
        fn = res.json() or {}
        self.facts.update(resource_arn=fn.get("FunctionArn"), package_type=fn.get("PackageType"),
                          runtime=fn.get("Runtime"), vpc=bool((fn.get("VpcConfig") or {}).get("SubnetIds")))
        role = fn.get("Role") or ""
        res = self.sh.run(["aws", "lambda", "list-functions", *o, "--query", "Functions[].Role"], timeout=120)
        users = sum(1 for r in (res.json() or []) if r == role) if res.ok else 1
        self.facts["role_users"] = users
        return role

    def _ecs(self, o, a):
        cluster = a.get("cluster") or ""
        td_ref = a["name"]
        if cluster:
            res = self.sh.run(["aws", "ecs", "describe-services", "--cluster", cluster, "--services", a["name"], *o],
                              timeout=90)
            svc = next((s for s in (res.json() or {}).get("services") or [] if s.get("status") != "INACTIVE"), None) \
                if res.ok else None
            if not res.ok:
                return self._aws_failed(res, f"ECS service '{a['name']}'")
            if svc is None:
                self.error("not_found", f"no ECS service '{a['name']}' in cluster {cluster}",
                           "Check the service name, or clear the cluster for a standalone task definition family.")
                return False
            td_ref = svc["taskDefinition"]
            self.facts.update(resource_arn=svc.get("serviceArn"), cluster=cluster, service=a["name"],
                              launch_type=svc.get("launchType"))
        res = self.sh.run(["aws", "ecs", "describe-task-definition", "--task-definition", td_ref, *o],
                          timeout=90, sensitive=True)
        if not res.ok:
            return self._aws_failed(res, f"ECS task definition '{td_ref}'")
        td = (res.json() or {}).get("taskDefinition") or {}
        self.facts.update(task_family=td.get("family"), task_definition=td.get("taskDefinitionArn"),
                          execution_role=td.get("executionRoleArn"))
        if "FARGATE" not in (td.get("requiresCompatibilities") or []) and "FARGATE" not in (td.get("compatibilities") or []):
            self.warn("not_fargate", "the task definition is not Fargate-compatible; EC2 tasks work the same way "
                      "but are not what this kind was proven on")
        role = td.get("taskRoleArn") or ""
        if role and role == td.get("executionRoleArn"):
            # The execution role pulls images and writes logs for the agent; never map it.
            self.facts["role_is_execution_role"] = True
            self.facts["role_users"] = 2
        return role

    def _apprunner(self, o, a):
        res = self.sh.run(["aws", "apprunner", "list-services", *o], timeout=90)
        if not res.ok:
            return self._aws_failed(res, "App Runner services")
        arn = next((s.get("ServiceArn") for s in (res.json() or {}).get("ServiceSummaryList") or []
                    if s.get("ServiceName") == a["name"]), None)
        if not arn:
            self.error("not_found", f"no App Runner service '{a['name']}' in {a['region']}", "Check the name and region.")
            return False
        res = self.sh.run(["aws", "apprunner", "describe-service", "--service-arn", arn, *o], timeout=90, sensitive=True)
        if not res.ok:
            return self._aws_failed(res, f"App Runner service '{a['name']}'")
        svc = (res.json() or {}).get("Service") or {}
        self.facts.update(resource_arn=arn, url=svc.get("ServiceUrl"))
        return (svc.get("InstanceConfiguration") or {}).get("InstanceRoleArn") or ""

    def _aws_identity(self, o, role_arn: str):
        dedicated = model.dedicated_identity_name(self.cfg)
        self.facts["role_arn"] = role_arn or None
        if role_arn and role_name(role_arn) == dedicated:
            # Created by this console for this app earlier: keep it in the workspace.
            self.facts.update(dedicated_identity=dedicated, mapping_subject=None)
            return
        shared = (not role_arn or self.facts.get("role_users", 1) > 1
                  or SHARED_ROLE_NAMES.match(role_name(role_arn)) or self.facts.get("role_is_execution_role"))
        if shared:
            why = ("has no role of its own" if not role_arn else
                   "uses the task execution role" if self.facts.get("role_is_execution_role") else
                   f"shares {role_name(role_arn)} with {self.facts.get('role_users', 2) - 1} other workload(s)")
            self.facts.update(dedicated_identity=dedicated, mapping_subject=None)
            policies = self._role_policies(o, role_arn) if role_arn else []
            self.facts["replaced_role_policies"] = policies
            self.warn("dedicated_identity", f"{self.facts['name']} {why}; Wire switches it to a new role {dedicated}",
                      "The new role gets only the JWT permission"
                      + (" and basic Lambda logging" if self.kind == "aws_lambda" else "")
                      + ". The current role has " + (", ".join(policies) if policies else "no visible policies")
                      + ": grant the new one what the app needs before you approve Wire.")
            return
        res = self.sh.run(["aws", "iam", "get-role", "--role-name", role_name(role_arn), *o], timeout=60)
        if not res.ok:
            self.error("role_lookup_failed", f"could not read role {role_arn}: {res.error_text(200)}",
                       "The mapping trusts this role's ARN: you need iam:GetRole on it.")
            return
        r = (res.json() or {}).get("Role") or {}
        self.facts.update(role_id=r.get("RoleId"), mapping_subject=r.get("Arn") or role_arn)

    def _role_policies(self, o, role_arn) -> list:
        res = self.sh.run(["aws", "iam", "list-attached-role-policies", "--role-name", role_name(role_arn), *o], timeout=60)
        return sorted(p.get("PolicyName") for p in (res.json() or {}).get("AttachedPolicies") or [] if p.get("PolicyName")) \
            if res.ok else []


# ------------------------------------------------------------------------ Wire


class AwsWire:
    def _aws_env(self) -> dict:
        provider, audience = self._need("jfrog_provider_name", "jfrog_audience")
        return {"JFROG_OIDC_AUDIENCE": audience, "JFROG_OIDC_AWS_REGION": model.get_path(self.cfg, "target.aws.region"),
                "JFROG_OIDC_PROVIDER": provider, "JFROG_OIDC_SOURCE": "aws", "JFROG_URL": self.jfrog_url}

    def _aws_role(self) -> str | None:
        return self.dedicated["arn"] if self.dedicated else None

    def _aws_target_id(self) -> str:
        a = model.get_path(self.cfg, "target.aws")
        return self.d.get("resource_arn") or f"aws:{a['account']}:{a['region']}:{a['name']}"

    def _aws(self, env):
        self._aws_cache = {}
        return {"aws_lambda": self._lambda_plan, "aws_ecs_task": self._ecs_plan,
                "aws_app_runner": self._apprunner_plan}[self.kind](env)

    def _env_diff(self, current: dict, env: dict):
        """Managed keys only; the other variables are counted, never read into the plan."""
        diff = [_entry_env(k, current.get(k), v) for k, v in env.items()]
        return diff, [d for d in diff if d["action"] != "same"]

    def _role_diff(self, diff, current_role):
        want = self._aws_role()
        if not want:
            return False
        e = {"scope": "identity", "setting": "IAM role", "current": current_role or None, "desired": want,
             "action": "same" if current_role == want else "add" if not current_role else "change"}
        diff.insert(0, e)
        return e["action"] != "same"

    def _read(self, key, argv):
        """Sensitive read, cached for one plan; apply() re-reads through a new plan."""
        if key not in self._aws_cache:
            res = self.sh.run(argv, timeout=120, sensitive=True)
            if not res.ok:
                raise WireError(f"could not read the target: {res.error_text(300)}")
            self._aws_cache[key] = res.json() or {}
        return self._aws_cache[key]

    # -- Lambda ---------------------------------------------------------------------

    def _lambda_get(self):
        name = model.get_path(self.cfg, "target.aws.name")
        return self._read("lambda", ["aws", "lambda", "get-function-configuration", "--function-name", name,
                                     *opts(self.cfg)])

    def _lambda_plan(self, env):
        fn = self._lambda_get()
        cur = ((fn.get("Environment") or {}).get("Variables")) or {}
        diff, changed = self._env_diff(cur, env)
        role_changes = self._role_diff(diff, fn.get("Role"))
        name = model.get_path(self.cfg, "target.aws.name")
        commands = []
        if changed or role_changes:
            argv = ["aws", "lambda", "update-function-configuration", "--function-name", name]
            if changed:
                argv += ["--environment", RESOLVE + "lambda-environment"]
            if role_changes:
                argv += ["--role", self._aws_role()]
            commands = [argv + opts(self.cfg),
                        ["aws", "lambda", "wait", "function-updated", "--function-name", name, *opts(self.cfg)]]
        notes = self._aws_notes(len(set(cur) - set(env)))
        return diff, commands, notes

    # -- ECS --------------------------------------------------------------------------

    def _ecs_taskdef(self):
        ref = self.d.get("task_family") or model.get_path(self.cfg, "target.aws.name")
        a = model.get_path(self.cfg, "target.aws")
        if a.get("cluster"):
            svc = self._read("ecs-service", ["aws", "ecs", "describe-services", "--cluster", a["cluster"],
                                             "--services", a["name"], *opts(self.cfg)])
            ref = next((s["taskDefinition"] for s in svc.get("services") or [] if s.get("status") != "INACTIVE"), ref)
        return self._read("ecs-taskdef", ["aws", "ecs", "describe-task-definition", "--task-definition", ref,
                                          *opts(self.cfg)]).get("taskDefinition") or {}

    def _ecs_container(self, td):
        cs = td.get("containerDefinitions") or []
        name = model.get_path(self.cfg, "target.aws.container") or ""
        return next((c for c in cs if c.get("name") == name), None) or next((c for c in cs if c.get("essential", True)), {})

    def _ecs_plan(self, env):
        td = self._ecs_taskdef()
        c = self._ecs_container(td)
        cur = {e.get("name"): e.get("value") for e in c.get("environment") or []}
        diff, changed = self._env_diff(cur, env)
        for d in diff:
            d["scope"] = f"env ({c.get('name')})"
        role_changes = self._role_diff(diff, td.get("taskRoleArn"))
        commands = []
        a = model.get_path(self.cfg, "target.aws")
        if changed or role_changes:
            commands.append(["aws", "ecs", "register-task-definition", "--cli-input-json", RESOLVE + "ecs-taskdef",
                             *opts(self.cfg)])
            if a.get("cluster"):
                # A family without a revision is its latest ACTIVE revision: the one just registered.
                commands.append(["aws", "ecs", "update-service", "--cluster", a["cluster"], "--service", a["name"],
                                 "--task-definition", td.get("family") or a["name"], *opts(self.cfg)])
        notes = self._aws_notes(len(set(cur) - set(env)))
        if not a.get("cluster"):
            notes.append(f"Standalone task family {td.get('family')}: schedules or callers that pin a revision "
                         "(EventBridge targets, run-task --task-definition family:N) need the new revision.")
        return diff, commands, notes

    # -- App Runner ---------------------------------------------------------------------

    def _apprunner_get(self):
        arn = self.d.get("resource_arn")
        if not arn:
            raise WireError("the App Runner service ARN is unknown: run Discover again")
        return self._read("apprunner", ["aws", "apprunner", "describe-service", "--service-arn", arn,
                                        *opts(self.cfg)]).get("Service") or {}

    @staticmethod
    def _apprunner_env_block(src: dict) -> dict:
        if src.get("ImageRepository"):
            return src["ImageRepository"].setdefault("ImageConfiguration", {})
        code = src.get("CodeRepository") or {}
        return code.setdefault("CodeConfiguration", {}).setdefault("CodeConfigurationValues", {})

    def _apprunner_plan(self, env):
        svc = self._apprunner_get()
        src = json.loads(json.dumps(svc.get("SourceConfiguration") or {}))
        cur = self._apprunner_env_block(src).get("RuntimeEnvironmentVariables") or {}
        diff, changed = self._env_diff(cur, env)
        role_changes = self._role_diff(diff, (svc.get("InstanceConfiguration") or {}).get("InstanceRoleArn"))
        commands = []
        if changed or role_changes:
            argv = ["aws", "apprunner", "update-service", "--service-arn", self.d["resource_arn"]]
            if changed:
                argv += ["--source-configuration", RESOLVE + "apprunner-source"]
            if role_changes:
                argv += ["--instance-configuration", RESOLVE + "apprunner-instance"]
            commands.append(argv + opts(self.cfg))
        notes = self._aws_notes(len(set(cur) - set(env)))
        notes.append("App Runner deploys the change asynchronously (a few minutes); Wire's check reads the new "
                     "configuration, the running instances follow when the deployment finishes.")
        return diff, commands, notes

    # -- shared ---------------------------------------------------------------------------

    def _aws_notes(self, others: int) -> list:
        notes = []
        if others:
            notes.append(f"{others} other environment variable(s) are kept as they are (not shown).")
        if self.dedicated:
            pol = self.d.get("replaced_role_policies") or []
            notes.append(f"The app moves to the role {self.dedicated['arn']}, which only has the JWT permission"
                         + (" and basic logging" if self.kind == "aws_lambda" else "")
                         + (f" (the old role had {', '.join(pol)})" if pol else "")
                         + ". Grant it what the app needs, and use it in your next deploy.")
        notes.append("Add these variables to the app's own IaC or deploy pipeline so the next deploy keeps them.")
        return notes

    def _aws_resolve(self, argv: list) -> list:
        """Fill the placeholders from a fresh sensitive read (whole environments are never stored)."""
        if not any(a.startswith(RESOLVE) for a in argv):
            return argv
        self._aws_cache = {}
        env = self.desired_env()
        out = []
        for a in argv:
            what = a[len(RESOLVE):] if a.startswith(RESOLVE) else None
            if what == "lambda-environment":
                cur = (self._lambda_get().get("Environment") or {}).get("Variables") or {}
                out.append(_json({"Variables": {**cur, **env}}))
            elif what == "ecs-taskdef":
                td = {k: v for k, v in self._ecs_taskdef().items() if k not in TASKDEF_READONLY}
                c = self._ecs_container(td)
                rest = [e for e in c.get("environment") or [] if e.get("name") not in env]
                c["environment"] = rest + [{"name": k, "value": v} for k, v in sorted(env.items())]
                if self._aws_role():
                    td["taskRoleArn"] = self._aws_role()
                out.append(_json(td))
            elif what == "apprunner-source":
                src = json.loads(json.dumps(self._apprunner_get().get("SourceConfiguration") or {}))
                blk = self._apprunner_env_block(src)
                blk["RuntimeEnvironmentVariables"] = {**(blk.get("RuntimeEnvironmentVariables") or {}), **env}
                out.append(_json(src))
            elif what == "apprunner-instance":
                inst = dict(self._apprunner_get().get("InstanceConfiguration") or {})
                inst["InstanceRoleArn"] = self._aws_role()
                out.append(_json(inst))
            elif what is not None:
                raise WireError(f"unknown placeholder {a}")
            else:
                out.append(a)
        return out


def _entry_env(key, current, desired):
    action = "same" if current == desired else "add" if current is None else "change"
    return {"scope": "env", "setting": key, "current": current, "desired": desired, "action": action}


def WireError(message):  # noqa: N802 - raised like the class it returns
    from .wire import WireError as _WireError   # wire imports this module
    return _WireError(message)
