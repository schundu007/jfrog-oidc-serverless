# Azure foundation for the ADO pipelines (apply once, locally): federated credentials on the
# service connections' managed identities, the setup identity's roles, and the Terraform
# state storage of the Azure stacks. No GCP or JFrog resources here: the pipelines'
# JFrog trust is OIDC configuration, made afterwards by terraform/stacks/oidc/azure/azure-jfrog-trust.

locals {
  issuer = "https://login.microsoftonline.com/${var.azure_tenant_id}/v2.0"
  # Entra federated credentials always use api://AzureADTokenExchange.
  audience = "api://AzureADTokenExchange"
}

#
# build: rights come from each workload stack (AcrPush, Contributor on the workload's
#   resource group).
# setup: creates workload stacks. Contributor on the subscription, role
#   assignments limited to AcrPull/AcrPush/Contributor, Entra apps it owns.

# The foundation's own resource group and the build identity: nothing has to exist beforehand.
resource "azurerm_resource_group" "ado" {
  name     = var.azure_identity.resource_group
  location = var.azure_location
}

resource "azurerm_user_assigned_identity" "ado" {
  name                = var.azure_identity.name
  location            = azurerm_resource_group.ado.location
  resource_group_name = azurerm_resource_group.ado.name
}

resource "azurerm_user_assigned_identity" "setup" {
  name                = "${var.azure_identity.name}-setup"
  location            = azurerm_resource_group.ado.location
  resource_group_name = azurerm_resource_group.ado.name
}

locals {
  azure_identity = {
    build = { id = azurerm_user_assigned_identity.ado.id, principal_id = azurerm_user_assigned_identity.ado.principal_id, client_id = azurerm_user_assigned_identity.ado.client_id }
    setup = { id = azurerm_user_assigned_identity.setup.id, principal_id = azurerm_user_assigned_identity.setup.principal_id, client_id = azurerm_user_assigned_identity.setup.client_id }
  }
  # AcrPull, AcrPush, Contributor, and the function host storage roles: Storage Blob Data Owner,
  # Storage Queue Data Contributor, Storage Table Data Contributor, Storage Blob Data Contributor (deployer)
  assignable_roles = join(", ", [
    "7f951dda-4ed3-4680-a7ca-43fe172d538d", "8311e382-0749-4cb8-b61a-304f252e45ec", "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b", "974c5e8b-45b9-4653-ba55-5f855dd0fb88", "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
  ])
}

resource "azurerm_federated_identity_credential" "ado" {
  for_each = local.connections

  name                      = "ado-${each.value.name}"
  user_assigned_identity_id = local.azure_identity[each.key].id
  issuer                    = local.issuer
  subject                   = each.value.subject
  audience                  = [local.audience]

  depends_on = [time_sleep.credential_propagation]
}

# AzureCLI@2 logs in before any script runs, so the build identity must see the
# subscription; Reader on its own resource group is the least that does.
resource "azurerm_role_assignment" "ado_login" {
  scope                = azurerm_resource_group.ado.id
  role_definition_name = "Reader"
  principal_id         = local.azure_identity.build.principal_id
  principal_type       = "ServicePrincipal"
}

data "azurerm_subscription" "this" {}

resource "azurerm_role_assignment" "setup_contributor" {
  scope                = data.azurerm_subscription.this.id
  role_definition_name = "Contributor"
  principal_id         = local.azure_identity.setup.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "setup_rbac" {
  scope                = data.azurerm_subscription.this.id
  role_definition_name = "Role Based Access Control Administrator"
  principal_id         = local.azure_identity.setup.principal_id
  principal_type       = "ServicePrincipal"
  condition_version    = "2.0"
  condition            = <<-EOT
    ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${local.assignable_roles}}))
    AND
    ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${local.assignable_roles}}))
  EOT
}

# Entra: create and manage only the apps it owns (the workloads' audience apps).
data "azuread_service_principal" "graph" {
  client_id = "00000003-0000-0000-c000-000000000000"
}

resource "azuread_app_role_assignment" "setup_apps" {
  app_role_id         = data.azuread_service_principal.graph.app_role_ids["Application.ReadWrite.OwnedBy"]
  principal_object_id = local.azure_identity.setup.principal_id
  resource_object_id  = data.azuread_service_principal.graph.object_id
}

# Terraform state for Azure stacks: Entra auth only, versioned.
resource "azurerm_storage_account" "tfstate" {
  name                            = substr("st${replace(var.ado_org, "-", "")}tfstate", 0, 24)
  resource_group_name             = azurerm_resource_group.ado.name
  location                        = azurerm_resource_group.ado.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  default_to_oauth_authentication = true
  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}

resource "azurerm_role_assignment" "setup_state" {
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = local.azure_identity.setup.principal_id
  principal_type       = "ServicePrincipal"
}


# Operators are given by email. A member's sign-in name is the email; a guest's (a personal account
# invited to the tenant) is <email with @ as _>#EXT#@<the tenant's default domain>. Both are tried.
data "azuread_domains" "default" {
  only_default = true
}

locals {
  operator_upns = {
    for e in var.azure_console_operators : e => [
      e, "${replace(e, "@", "_")}#EXT#@${data.azuread_domains.default.domains[0].domain_name}",
    ]
  }
}

data "azuread_users" "console_operators" {
  user_principal_names = flatten(values(local.operator_upns))
  ignore_missing       = true
}

locals {
  operator_ids = {
    for e, upns in local.operator_upns : e => one([
      for u in data.azuread_users.console_operators.users : u.object_id if contains([for x in upns : lower(x)], lower(u.user_principal_name))
    ])
  }
}

resource "azurerm_role_assignment" "console_operator_state" {
  for_each             = toset(var.azure_console_operators)
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = local.operator_ids[each.value]
  principal_type       = "User"

  lifecycle {
    precondition {
      condition     = local.operator_ids[each.value] != null
      error_message = "No user in the tenant has the email ${each.value}."
    }
  }
}
