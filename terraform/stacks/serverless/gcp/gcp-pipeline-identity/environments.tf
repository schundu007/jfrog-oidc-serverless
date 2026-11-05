# One ADO environment per workload stack of this cloud: each stack's stack-setup pipeline applies
# only after approval on <stack>-<pipeline_environment>. Approvers: ado_approvers (user principal
# names); empty leaves the environments without a check.

# The stacks with a stack-setup pipeline (serverless workloads and the test cluster). Fixed by the
# repo, not a target input: pipelines/<kind>/gcp/<stack>/stack-setup.yml exists for each.
locals {
  pipeline_stacks = ["gcp-cloud-run", "gcp-cloud-run-service", "gcp-cloud-run-function", "gcp-cloud-run-worker-pool", "gke-cluster"]
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
  for_each    = toset(local.pipeline_stacks)
  project_id  = data.azuredevops_project.this.id
  name        = "${each.value}-${var.pipeline_environment}"
  description = "Approval for terraform/stacks/${each.value == "gke-cluster" ? "kubernetes" : "serverless"}/gcp/${each.value} (${var.pipeline_environment})."
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
