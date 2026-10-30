"""AWS targets: Discover, Preflight and Wire against doc-derived fixtures (no AWS account)."""

import json
import unittest
from pathlib import Path

from console import model
from console.shell import Shell, load_fixtures
from console.steps import aws, discover, preflight, wire

FX = Path(__file__).parent / "fixtures"
A, R = "123456789012", "us-east-1"
O = ["--region", R, "--output", "json"]
ISS = "https://a1d2b0fd-1177-4468-9351-2feeeeeee723.tokens.sts.global.api.aws"
OUT = {"jfrog_provider_name": f"aws-{A}", "jfrog_audience": "jfrog-aws"}
SECRET = "not-for-the-console"


def cfg(kind, name, **target):
    c = model.default_config(kind, name=name)
    c["target"]["aws"].update(account=A, region=R, name=name, **target)
    c["jfrog"]["url"] = "https://acme.jfrog.io"
    return c


def discovered(kind, name, fixture, **target):
    c = cfg(kind, name, **target)
    sh = Shell.replay(FX / "discover" / f"{fixture}.json")
    res = discover.run(c, sh)
    return c, res, sh


class DiscoverTests(unittest.TestCase):
    def test_lambda_own_role(self):
        _, res, sh = discovered("aws_lambda", "orders-api", "aws_lambda")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["facts"]["mapping_subject"], f"arn:aws:iam::{A}:role/orders-api-role")
        self.assertEqual(res["facts"]["issuer"], ISS)
        self.assertEqual(res["config_patch"]["jfrog.integration_names.aws"], f"aws-{A}")
        self.assertNotIn(SECRET, json.dumps(res))

    def test_lambda_read_is_sensitive(self):
        sh = Shell(mode="record", fixtures=[], which=lambda n: None)
        sh.mode = "record"
        discover.run(cfg("aws_lambda", "orders-api"), sh)
        # aws is not installed here: the first call fails; nothing it printed is recorded.
        self.assertTrue(all(r["stdout"] in ("", "[REDACTED: sensitive output not recorded]") for r in sh.recorded))

    def test_lambda_shared_role_gets_dedicated(self):
        _, res, _ = discovered("aws_lambda", "billing", "aws_lambda_shared_role")
        f = res["facts"]
        self.assertEqual(f["dedicated_identity"], "jfrog-billing")
        self.assertIsNone(f["mapping_subject"])
        self.assertEqual(f["replaced_role_policies"], ["AWSLambdaBasicExecutionRole", "orders-dynamodb"])
        self.assertIn("dedicated_identity", [w["code"] for w in res["warnings"]])

    def test_ecs_service(self):
        _, res, sh = discovered("aws_ecs_task", "orders-svc", "aws_ecs_service", cluster="apps")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["facts"]["mapping_subject"], f"arn:aws:iam::{A}:role/orders-svc-task")
        self.assertEqual(res["facts"]["task_family"], "orders-svc")

    def test_ecs_task_without_role_gets_dedicated(self):
        _, res, _ = discovered("aws_ecs_task", "nightly-export", "aws_ecs_task_no_role")
        self.assertEqual(res["facts"]["dedicated_identity"], "jfrog-nightly-export")
        self.assertEqual(res["errors"], [])

    def test_app_runner(self):
        _, res, sh = discovered("aws_app_runner", "orders-web", "aws_app_runner")
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["facts"]["mapping_subject"], f"arn:aws:iam::{A}:role/orders-web-instance")
        self.assertTrue(res["facts"]["resource_arn"].startswith("arn:aws:apprunner:"))

    def test_federation_disabled_fails_with_enable_command(self):
        _, res, _ = discovered("aws_lambda", "orders-api", "aws_federation_disabled")
        self.assertEqual(res["status"], "fail")
        self.assertEqual(res["errors"][0]["code"], "federation_disabled")
        self.assertIn("aws iam enable-outbound-web-identity-federation --region us-east-1", res["errors"][0]["fix"])

    def test_not_found(self):
        _, res, _ = discovered("aws_lambda", "nope", "aws_not_found")
        self.assertEqual(res["errors"][0]["code"], "not_found")

    def test_dedicated_role_recognised_later(self):
        c = cfg("aws_lambda", "billing")
        sh = Shell(mode="replay", fixtures=[
            *load_fixtures(FX / "discover" / "aws_lambda.json")[:2],
            {"argv": ["aws", "lambda", "get-function-configuration", "--function-name", "billing", *O],
             "stdout": json.dumps({"Role": f"arn:aws:iam::{A}:role/jfrog-billing"})},
            {"argv": ["aws", "lambda", "list-functions", *O, "--query", "Functions[].Role"], "stdout": "[]"}])
        res = discover.run(c, sh)
        self.assertEqual(res["facts"]["dedicated_identity"], "jfrog-billing")


class PreflightTests(unittest.TestCase):
    def run_pf(self, extra=(), env=None):
        sh = Shell(mode="replay", fixtures=[*extra, *load_fixtures(FX / "preflight" / "aws_ok.json")])
        return preflight.run(cfg("aws_lambda", "orders-api"), sh, env=env or {"JFROG_ACCESS_TOKEN": "t" * 20}), sh

    SIM = ["aws", "iam", "simulate-principal-policy", "--policy-source-arn",
           f"arn:aws:iam::{A}:role/AWSReservedSSO_AdministratorAccess_0123", "--action-names",
           *aws.PERMISSIONS_COMMON, *aws.PERMISSIONS_BY_KIND["aws_lambda"], *O]

    def test_all_green(self):
        allowed = {"EvaluationResults": [{"EvalActionName": a, "EvalDecision": "allowed"}
                                         for a in aws.PERMISSIONS_COMMON + aws.PERMISSIONS_BY_KIND["aws_lambda"]]}
        res, sh = self.run_pf([{"argv": self.SIM, "stdout": json.dumps(allowed)}])
        self.assertEqual(sh.misses, [])
        self.assertEqual(res["status"], "ok", [c for c in res["checks"] if c["status"] != "ok"])
        self.assertEqual(res["facts"]["aws_issuer"], ISS)

    def test_federation_off_fails_with_fix(self):
        off = {"argv": ["aws", "iam", "get-outbound-web-identity-federation-info", *O],
               "stdout": json.dumps({"JwtVendingEnabled": False})}
        res, _ = self.run_pf([off])
        c = {c["id"]: c for c in res["checks"]}["aws_federation"]
        self.assertEqual(c["status"], "fail")
        self.assertEqual(c["fix"], "Run once per account: aws iam enable-outbound-web-identity-federation --region us-east-1")

    def test_recorded_feature_disabled(self):
        """Recorded live on a test account: FeatureDisabled, exit 254, root login."""
        sh = Shell.replay(FX / "preflight" / "aws_federation_disabled.json")
        res = preflight.run(cfg("aws_lambda", "orders-api"), sh, env={"JFROG_ACCESS_TOKEN": "t" * 20})
        checks = {c["id"]: c for c in res["checks"]}
        self.assertEqual(checks["aws_federation"]["status"], "fail")
        self.assertIn("FeatureDisabled", checks["aws_federation"]["detail"])
        self.assertEqual(checks["aws_federation"]["fix"],
                         "Run once per account: aws iam enable-outbound-web-identity-federation --region us-east-1")
        self.assertEqual(checks["aws_login"]["status"], "warn")       # root user
        self.assertNotIn("aws_permissions", checks)                   # simulation skipped for root
        self.assertEqual(res["status"], "fail")

    def test_denied_actions_warn(self):
        denied = {"EvaluationResults": [{"EvalActionName": "iam:CreateRole", "EvalDecision": "implicitDeny"}]}
        res, _ = self.run_pf([{"argv": self.SIM, "stdout": json.dumps(denied)}])
        c = {c["id"]: c for c in res["checks"]}["aws_permissions"]
        self.assertEqual(c["status"], "warn")
        self.assertIn("iam:CreateRole", c["detail"])

    def test_wrong_account(self):
        other = {"argv": ["aws", "sts", "get-caller-identity", *O],
                 "stdout": json.dumps({"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/x"})}
        res, _ = self.run_pf([other])
        self.assertEqual({c["id"]: c for c in res["checks"]}["aws_login"]["status"], "fail")

    def test_expired_login(self):
        expired = {"argv": ["aws", "sts", "get-caller-identity", *O], "rc": 255,
                   "stderr": "Error when retrieving token from sso: Token has expired and refresh failed"}
        res, _ = self.run_pf([expired])
        c = {c["id"]: c for c in res["checks"]}["aws_login"]
        self.assertEqual(c["status"], "fail")
        self.assertIn("aws sso login", c["fix"])

    def test_integration_for_other_issuer_fails(self):
        c = cfg("aws_lambda", "orders-api")
        c["jfrog"]["integration_names"]["aws"] = "aws-sts"
        fx = [f if f.get("http") != "GET https://acme.jfrog.io/access/api/v1/oidc" else
              {**f, "body": json.dumps([{"name": "aws-sts", "issuer_url": "https://other.tokens.sts.global.api.aws"}])}
              for f in load_fixtures(FX / "preflight" / "aws_ok.json")]
        res = preflight.run(c, Shell(mode="replay", fixtures=fx), env={"JFROG_ACCESS_TOKEN": "t" * 20})
        self.assertEqual({c["id"]: c for c in res["checks"]}["jfrog_integration"]["status"], "fail")


def lambda_get(env, role=f"arn:aws:iam::{A}:role/orders-api-role", consume=False):
    return {"argv": ["aws", "lambda", "get-function-configuration", "--function-name", "orders-api", *O],
            "stdout": json.dumps({"FunctionArn": f"arn:aws:lambda:{R}:{A}:function:orders-api", "Role": role,
                                  "Environment": {"Variables": env}}), "consume": consume}


def wired_cfg(kind="aws_lambda", name="orders-api", **facts):
    c = cfg(kind, name)
    c["target"]["discovered"] = {"kind": kind, "mapping_subject": f"arn:aws:iam::{A}:role/x", **facts}
    return c


class WireLambdaTests(unittest.TestCase):
    def test_plan_never_holds_other_values(self):
        sh = Shell(mode="replay", fixtures=[lambda_get({"DB_PASSWORD": SECRET})])
        plan = wire.Wire(wired_cfg(), sh, OUT).plan()
        text = json.dumps(plan)
        self.assertNotIn(SECRET, text)
        self.assertEqual({d["setting"] for d in plan["diff"]},
                         {"JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SOURCE", "JFROG_OIDC_AWS_REGION"})
        self.assertIn("@resolve:lambda-environment", plan["commands"][0])
        self.assertTrue(any("1 other environment variable" in n for n in plan["notes"]))

    def test_apply_merges_environment_and_verifies(self):
        c = wired_cfg()
        desired = wire.Wire(c, Shell(mode="replay"), OUT).desired_env()
        merged = aws._json({"Variables": {"DB_PASSWORD": SECRET, **desired}})
        update = ["aws", "lambda", "update-function-configuration", "--function-name", "orders-api",
                  "--environment", merged, *O]
        sh = Shell(mode="replay", fixtures=[
            lambda_get({"DB_PASSWORD": SECRET}, consume=True),     # plan shown to the operator
            lambda_get({"DB_PASSWORD": SECRET}, consume=True),     # apply's fresh plan
            lambda_get({"DB_PASSWORD": SECRET}, consume=True),     # placeholder fill
            {"argv": update, "stdout": "{}"},
            {"argv": ["aws", "lambda", "wait", "function-updated", "--function-name", "orders-api", *O], "stdout": ""},
            lambda_get({"DB_PASSWORD": SECRET, **desired})])
        w = wire.Wire(c, sh, OUT)
        plan = w.plan()
        res = w.apply(plan, {"diff_sha256": plan["sha256"]})
        self.assertEqual(sh.misses, [])
        self.assertTrue(res["verified"])
        self.assertNotIn(SECRET, json.dumps(res))

    def test_dedicated_role_switch(self):
        c = wired_cfg(name="orders-api", dedicated_identity="jfrog-orders-api", mapping_subject=None)
        out = {**OUT, "dedicated_roles": {"orders-api": f"arn:aws:iam::{A}:role/jfrog-orders-api"}}
        sh = Shell(mode="replay", fixtures=[lambda_get({})])
        plan = wire.Wire(c, sh, out).plan()
        self.assertEqual(plan["diff"][0]["setting"], "IAM role")
        self.assertIn("--role", plan["commands"][0])
        self.assertIn(f"arn:aws:iam::{A}:role/jfrog-orders-api", plan["commands"][0])

    def test_noop(self):
        c = wired_cfg()
        desired = wire.Wire(c, Shell(mode="replay"), OUT).desired_env()
        sh = Shell(mode="replay", fixtures=[lambda_get(desired)])
        self.assertTrue(wire.Wire(c, sh, OUT).plan()["noop"])


class WireEcsAppRunnerTests(unittest.TestCase):
    TD = {"taskDefinitionArn": f"arn:aws:ecs:{R}:{A}:task-definition/orders-svc:7", "family": "orders-svc",
          "revision": 7, "status": "ACTIVE", "taskRoleArn": f"arn:aws:iam::{A}:role/orders-svc-task",
          "registeredAt": "2026-01-01T00:00:00Z", "requiresCompatibilities": ["FARGATE"],
          "containerDefinitions": [{"name": "app", "image": "img", "essential": True,
                                    "environment": [{"name": "API_KEY", "value": SECRET}]}]}

    def fx(self):
        svc = {"argv": ["aws", "ecs", "describe-services", "--cluster", "apps", "--services", "orders-svc", *O],
               "stdout": json.dumps({"services": [{"status": "ACTIVE", "taskDefinition": self.TD["taskDefinitionArn"]}]})}
        td = {"argv": ["aws", "ecs", "describe-task-definition", "--task-definition", self.TD["taskDefinitionArn"], *O],
              "stdout": json.dumps({"taskDefinition": self.TD})}
        return [svc, td]

    def test_ecs_registers_revision_then_updates_service(self):
        c = wired_cfg("aws_ecs_task", "orders-svc", task_family="orders-svc")
        c["target"]["aws"]["cluster"] = "apps"
        w = wire.Wire(c, Shell(mode="replay", fixtures=self.fx()), OUT)
        plan = w.plan()
        self.assertNotIn(SECRET, json.dumps(plan))
        self.assertEqual(plan["commands"][0][:3], ["aws", "ecs", "register-task-definition"])
        self.assertEqual(plan["commands"][1][:3], ["aws", "ecs", "update-service"])
        self.assertIn("orders-svc", plan["commands"][1])
        filled = json.loads(w._resolve(plan["commands"][0])[4])
        self.assertNotIn("revision", filled)
        self.assertNotIn("registeredAt", filled)
        env = {e["name"]: e["value"] for e in filled["containerDefinitions"][0]["environment"]}
        self.assertEqual(env["API_KEY"], SECRET)        # kept, only ever sent to AWS
        self.assertEqual(env["JFROG_OIDC_SOURCE"], "aws")

    def test_app_runner(self):
        arn = f"arn:aws:apprunner:{R}:{A}:service/orders-web/x"
        svc = {"Service": {"SourceConfiguration": {"ImageRepository": {"ImageIdentifier": "img", "ImageConfiguration":
                                                                       {"Port": "8080", "RuntimeEnvironmentVariables": {"TOKEN_SALT": SECRET}}}},
                           "InstanceConfiguration": {"Cpu": "1024", "InstanceRoleArn": f"arn:aws:iam::{A}:role/i"}}}
        c = wired_cfg("aws_app_runner", "orders-web", resource_arn=arn)
        w = wire.Wire(c, Shell(mode="replay", fixtures=[
            {"argv": ["aws", "apprunner", "describe-service", "--service-arn", arn, *O], "stdout": json.dumps(svc)}]), OUT)
        plan = w.plan()
        self.assertNotIn(SECRET, json.dumps(plan))
        argv = w._resolve(plan["commands"][0])
        src = json.loads(argv[argv.index("--source-configuration") + 1])
        rev = src["ImageRepository"]["ImageConfiguration"]["RuntimeEnvironmentVariables"]
        self.assertEqual(rev["TOKEN_SALT"], SECRET)
        self.assertEqual(rev["JFROG_URL"], "https://acme.jfrog.io")

    def test_display_commands_show_placeholder(self):
        c = wired_cfg("aws_ecs_task", "orders-svc", task_family="orders-svc")
        c["target"]["aws"]["cluster"] = "apps"
        plan = wire.Wire(c, Shell(mode="replay", fixtures=self.fx()), OUT).plan()
        self.assertIn("@resolve:ecs-taskdef", plan["display_commands"][0])


if __name__ == "__main__":
    unittest.main()
