# Every workload's resource group and every Azure role the pipelines and workloads need, scoped to that
# resource group. The pipelines cannot grant roles, so a new workload starts here: add it to workloads,
# apply (admin), then run its stack.
#   identity_type UserAssigned: id-<name> is made here with its runtime roles, before the workload exists.
#   identity_type SystemAssigned: after the workload's first deploy, set runtime_principal_id from its
#     stack's runtime_principal_id output and apply again (only types with runtime roles need this).

variable "workloads" {
  description = "Workloads by name (rg-<name>): stack is one of the Azure workload stacks; identity_type matches the workload stack's (SystemAssigned or UserAssigned); runtime_principal_id: a system-assigned identity's object ID, after its first deploy."
  type = map(object({
    stack                = string
    identity_type        = optional(string, "SystemAssigned")
    runtime_principal_id = optional(string, "")
  }))
  default = {}
  validation {
    condition     = alltrue([for w in values(var.workloads) : contains(keys(local.pipeline_stack_kind), w.stack) && contains(["SystemAssigned", "UserAssigned"], w.identity_type)])
    error_message = "workloads.<name>.stack must be one of the Azure workload stacks and identity_type SystemAssigned or UserAssigned."
  }
}

locals {
  # Roles by stack, on the workload's resource group only.
  build_roles = {
    "azure-container-app"     = ["Contributor", "AcrPush"]
    "azure-container-apps"    = ["Contributor", "AcrPush"]
    "azure-functions-aca"     = ["Contributor", "AcrPush", "Storage Blob Data Contributor"]
    "azure-functions-premium" = ["Contributor", "Storage Blob Data Contributor"]
    "azure-functions-flex"    = ["Contributor", "Storage Blob Data Contributor"]
    "azure-web-app"           = ["Contributor"]
  }
  host_storage = ["Storage Blob Data Owner", "Storage Queue Data Contributor", "Storage Table Data Contributor"]
  runtime_roles = {
    "azure-container-app"     = ["AcrPull"]
    "azure-container-apps"    = ["AcrPull"]
    "azure-functions-aca"     = concat(["AcrPull"], local.host_storage)
    "azure-functions-premium" = local.host_storage
    "azure-functions-flex"    = local.host_storage
    "azure-web-app"           = []
  }
  workload_grants = merge(
    { for w, v in var.workloads : "${w}/setup/Contributor" => { workload = w, principal_id = local.azure_identity.setup.principal_id, role = "Contributor" } },
    merge([for w, v in var.workloads : { for r in local.build_roles[v.stack] : "${w}/build/${r}" => { workload = w, principal_id = local.azure_identity.build.principal_id, role = r } }]...),
    merge([for w, v in local.runtime_principals : { for r in local.runtime_roles[var.workloads[w].stack] : "${w}/runtime/${r}" => { workload = w, principal_id = v, role = r } }]...),
  )
  user_assigned = { for w, v in var.workloads : w => v if v.identity_type == "UserAssigned" }
  runtime_principals = merge(
    { for w, v in var.workloads : w => v.runtime_principal_id if v.identity_type == "SystemAssigned" && v.runtime_principal_id != "" },
    { for w, v in local.user_assigned : w => azurerm_user_assigned_identity.workload[w].principal_id },
  )
}

resource "azurerm_resource_group" "workload" {
  for_each = var.workloads
  name     = "rg-${each.key}"
  location = var.azure_location
  tags     = { "jfrog-oidc-stack" = each.value.stack }
}

resource "azurerm_role_assignment" "workload" {
  for_each             = local.workload_grants
  scope                = azurerm_resource_group.workload[each.value.workload].id
  role_definition_name = each.value.role
  principal_id         = each.value.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_user_assigned_identity" "workload" {
  for_each            = local.user_assigned
  name                = "id-${each.key}"
  location            = azurerm_resource_group.workload[each.key].location
  resource_group_name = azurerm_resource_group.workload[each.key].name
}
