# One ADO environment per workload stack of this cloud: each stack's stack-setup pipeline applies
# only after approval on <stack>-<pipeline_environment>. Approvers: ado_approvers (user principal
# names); empty leaves the environments without a check.

variable "workload_stacks" {
  description = "This cloud's serverless workload stacks (one ADO environment each)."
  type        = list(string)
  default     = ["azure-container-apps", "azure-container-app", "azure-functions-aca", "azure-functions-premium", "azure-functions-flex"]
}

variable "ado_approvers" {
  description = "ADO users (principal names, e.g. someone@example.com) who approve each stack's apply. Empty: no approval check."
  type        = list(string)
  default     = []
}

data "azuredevops_identity_user" "approver" {
  for_each = toset(var.ado_approvers)
  name     = each.value
}

resource "azuredevops_environment" "stack" {
  for_each    = toset(var.workload_stacks)
  project_id  = data.azuredevops_project.this.id
  name        = "${each.value}-${var.pipeline_environment}"
  description = "Approval for terraform/stacks/serverless/azure/${each.value} (${var.pipeline_environment})."
}

resource "azuredevops_check_approval" "stack" {
  for_each                   = length(var.ado_approvers) == 0 ? {} : azuredevops_environment.stack
  project_id                 = data.azuredevops_project.this.id
  target_resource_id         = each.value.id
  target_resource_type       = "environment"
  approvers                  = [for a in data.azuredevops_identity_user.approver : a.id]
  minimum_required_approvers = 1
  requester_can_approve      = true
  timeout                    = 43200
}

# Pipelines may use the environments without a one-time "Permit" in ADO (a run otherwise waits on
# an authorization checkpoint before its approval). The approval check above still gates every apply.
resource "azuredevops_pipeline_authorization" "stack" {
  for_each    = azuredevops_environment.stack
  project_id  = data.azuredevops_project.this.id
  resource_id = each.value.id
  type        = "environment"
}
