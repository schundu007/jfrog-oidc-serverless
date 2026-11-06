# The Azure JFrog OIDC stacks' own stack-setup pipelines, registered here and never by azure-pipeline-identity
# (infrastructure knows nothing about them); none today. Each applies only after approval on its ADO
# environment <stack>-<pipeline_environment>.

locals {
  oidc_pipelines = {}
}

variable "ado_repository" {
  description = "ADO Git repository that holds the pipelines (pipelines/delivery.json)."
  type        = string
}

variable "ado_approvers" {
  description = "ADO users (principal names, e.g. someone@example.com) who approve each JFrog OIDC stack's apply. Empty: no approval check."
  type        = list(string)
  default     = []
}

data "azuredevops_git_repository" "pipelines" {
  project_id = data.azuredevops_project.this.id
  name       = var.ado_repository
}

resource "azuredevops_build_definition" "oidc" {
  for_each   = local.oidc_pipelines
  project_id = data.azuredevops_project.this.id
  name       = each.value.name
  path       = each.value.path

  ci_trigger {
    use_yaml = true
  }

  repository {
    repo_type   = "TfsGit"
    repo_id     = data.azuredevops_git_repository.pipelines.id
    branch_name = data.azuredevops_git_repository.pipelines.default_branch
    yml_path    = "pipelines/oidc/azure/${each.key}/stack-setup.yml"
  }
}

resource "azuredevops_environment" "oidc" {
  for_each    = local.oidc_pipelines
  project_id  = data.azuredevops_project.this.id
  name        = "${each.key}-${var.pipeline_environment}"
  description = "Approval for terraform/stacks/oidc/azure/${each.key} (${var.pipeline_environment})."
}

data "azuredevops_identity_user" "approver" {
  for_each = toset(var.ado_approvers)
  name     = each.value
}

resource "azuredevops_check_approval" "oidc" {
  for_each                   = length(var.ado_approvers) == 0 ? {} : azuredevops_environment.oidc
  project_id                 = data.azuredevops_project.this.id
  target_resource_id         = each.value.id
  target_resource_type       = "environment"
  approvers                  = [for a in data.azuredevops_identity_user.approver : a.id]
  minimum_required_approvers = 1
  requester_can_approve      = true
  timeout                    = 43200
}

# The pipeline may use its environment without a one-time "Permit" in ADO; the approval check above
# still gates every apply.
resource "azuredevops_pipeline_authorization" "oidc" {
  for_each    = azuredevops_environment.oidc
  project_id  = data.azuredevops_project.this.id
  resource_id = each.value.id
  type        = "environment"
}
