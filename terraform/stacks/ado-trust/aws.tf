# AWS trust for the ADO pipelines (off until aws_account_id is set).
#
# IAM trusts the same Entra-issued service connection token the other clouds
# take: an OIDC identity provider for the tenant's v2.0 issuer, and one role per
# connection that only that connection's sub can assume
# (sts:AssumeRoleWithWebIdentity). The pipeline scripts export AWS_ROLE_ARN and
# AWS_WEB_IDENTITY_TOKEN_FILE, which the AWS CLI and the Terraform provider read.
#
#   ado-build  pushes images to ECR and rolls Lambda / ECS / App Runner out
#   ado-setup  applies terraform/workspaces/aws_* (dedicated roles, their
#              GetWebIdentityToken policy) and runs the console's Wire step

locals {
  aws_enabled = var.aws_account_id != null
  # IAM condition keys name the provider by its URL without https://.
  aws_issuer_key = trimprefix(local.issuer, "https://")
  aws_roles      = local.aws_enabled ? var.connections : {}
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = local.aws_enabled ? [var.aws_account_id] : null
  # With AWS off, nothing in this file is created and no credentials are needed.
  skip_credentials_validation = !local.aws_enabled
  skip_requesting_account_id  = !local.aws_enabled
  skip_metadata_api_check     = !local.aws_enabled
}

resource "aws_iam_openid_connect_provider" "ado" {
  count          = local.aws_enabled ? 1 : 0
  url            = local.issuer
  client_id_list = [local.audience]
}

data "aws_iam_policy_document" "ado_assume" {
  for_each = local.aws_roles
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.ado[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.aws_issuer_key}:aud"
      values   = [local.audience]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.aws_issuer_key}:sub"
      values   = [each.value.subject]
    }
  }
}

resource "aws_iam_role" "ado" {
  for_each             = local.aws_roles
  name                 = "ado-${each.key}"
  description          = "Azure DevOps ${each.value.name} (${var.ado_org})"
  assume_role_policy   = data.aws_iam_policy_document.ado_assume[each.key].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "ado_build" {
  count = local.aws_enabled ? 1 : 0
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken", "sts:GetCallerIdentity"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:PutImage",
    "ecr:UploadLayerPart"]
    resources = ["arn:aws:ecr:${var.aws_region}:${var.aws_account_id}:repository/${var.aws_image_repository}"]
  }
  statement {
    sid = "Rollout"
    actions = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:InvokeFunction",
      "ecs:DescribeServices", "ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition", "ecs:UpdateService",
    "ecs:RunTask", "ecs:DescribeTasks", "apprunner:DescribeService", "apprunner:StartDeployment"]
    resources = ["*"]
  }
  statement {
    sid       = "PassWorkloadRoles"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${var.aws_account_id}:role/jfrog-*"]
  }
}

data "aws_iam_policy_document" "ado_setup" {
  count = local.aws_enabled ? 1 : 0
  statement {
    sid = "Read"
    actions = ["sts:GetCallerIdentity", "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies", "iam:ListAccountAliases", "iam:GetOutboundWebIdentityFederationInfo",
      "lambda:GetFunctionConfiguration", "lambda:ListFunctions", "ecs:Describe*", "ecs:List*", "apprunner:Describe*",
    "apprunner:List*"]
    resources = ["*"]
  }
  statement {
    sid = "WorkloadRoles"
    actions = ["iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole",
      "iam:UntagRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
    "iam:ListInstanceProfilesForRole", "iam:PassRole"]
    resources = ["arn:aws:iam::${var.aws_account_id}:role/jfrog-*"]
  }
  statement {
    sid       = "ExistingRolePolicy"
    actions   = ["iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy"]
    resources = ["arn:aws:iam::${var.aws_account_id}:role/*"]
    condition {
      test     = "StringLike"
      variable = "iam:PolicyName"
      values   = ["jfrog-*"]
    }
  }
  statement {
    sid       = "Wire"
    actions   = ["lambda:UpdateFunctionConfiguration", "ecs:RegisterTaskDefinition", "ecs:UpdateService", "apprunner:UpdateService"]
    resources = ["*"]
  }
  statement {
    sid       = "State"
    actions   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [aws_s3_bucket.tfstate[0].arn, "${aws_s3_bucket.tfstate[0].arn}/workspaces/*"]
  }
}

resource "aws_iam_role_policy" "ado" {
  for_each = local.aws_roles
  name     = "ado-${each.key}"
  role     = aws_iam_role.ado[each.key].id
  policy   = each.key == "build" ? data.aws_iam_policy_document.ado_build[0].json : data.aws_iam_policy_document.ado_setup[0].json
}

resource "aws_s3_bucket" "tfstate" {
  count         = local.aws_enabled ? 1 : 0
  bucket        = "${var.aws_account_id}-${var.ado_org}-tfstate"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "tfstate" {
  count  = local.aws_enabled ? 1 : 0
  bucket = aws_s3_bucket.tfstate[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  count                   = local.aws_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.tfstate[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ecr_repository" "build" {
  count                = local.aws_enabled ? 1 : 0
  name                 = var.aws_image_repository
  image_tag_mutability = "IMMUTABLE"
}
