# AWS side of the trust: the roles that may ask STS for a JWT with the JFrog
# audience. The JWT's sub is the role ARN, which the JFrog identity mapping pins.
# No credential is stored anywhere: the workload signs the STS call with its
# own temporary role credentials.

locals {
  trust_principal = {
    aws_lambda     = "lambda.amazonaws.com"
    aws_ecs_task   = "ecs-tasks.amazonaws.com"
    aws_app_runner = "tasks.apprunner.amazonaws.com"
  }

  role_names = merge(var.existing_roles, { for k, r in aws_iam_role.dedicated : k => r.name })
}

data "aws_caller_identity" "current" {}

resource "aws_iam_outbound_web_identity_federation" "this" {
  count = var.enable_outbound_federation ? 1 : 0
}

data "aws_iam_policy_document" "assume" {
  for_each = var.dedicated_roles

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = [local.trust_principal[each.value.kind]]
    }
    # Only this account's service may assume the role (confused-deputy guard).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "dedicated" {
  for_each = var.dedicated_roles

  name               = each.value.name
  description        = "Dedicated runtime identity for ${each.key}, created by the JFrog OIDC console"
  assume_role_policy = data.aws_iam_policy_document.assume[each.key].json
  tags               = { "managed-by" = "jfrog-oidc-console", "workload" = each.key }
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  for_each = { for k, r in var.dedicated_roles : k => r if r.kind == "aws_lambda" }

  role       = aws_iam_role.dedicated[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "web_identity" {
  statement {
    sid       = "JFrogOidcWebIdentityToken"
    actions   = ["sts:GetWebIdentityToken"]
    resources = ["*"]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "sts:IdentityTokenAudience"
      values   = [var.audience]
    }
    condition {
      test     = "NumericLessThanEquals"
      variable = "sts:DurationSeconds"
      values   = [tostring(var.max_token_seconds)]
    }
  }
}

resource "aws_iam_role_policy" "web_identity" {
  for_each = local.role_names

  name   = "${var.name_prefix}-oidc-web-identity"
  role   = each.value
  policy = data.aws_iam_policy_document.web_identity.json
}
