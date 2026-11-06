# Azure pipeline identity for the ADO pipelines. An admin applies it with their own sign-in (Infrastructure
# page, or terraform locally), once and whenever a workload is added: federated credentials on the service
# connections' managed identities, every role grant (workloads.tf), and the Terraform state storage of the
# Azure stacks. No GCP or JFrog resources here: the pipelines'
# JFrog trust is OIDC configuration, made afterwards by terraform/stacks/oidc/azure/azure-jfrog-trust.

locals {
  issuer = "https://login.microsoftonline.com/${var.azure_tenant_id}/v2.0"
  # Entra federated credentials always use api://AzureADTokenExchange.
  audience = "api://AzureADTokenExchange"
}

#
# An admin applies this stack with their own sign-in; it is the only place Azure roles are granted.
# No pipeline identity can assign roles (no Owner, no Role Based Access Control Administrator).
# build: Contributor (+ AcrPush / Blob Data Contributor) on each workload's resource group (workloads.tf).
# setup: Contributor on each workload's resource group, the state store, and Entra apps it owns.

# The pipeline identity's own resource group and the build identity: nothing has to exist beforehand.
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

# AzureCLI@2 logs in before any script runs, so each pipeline identity must see the
# subscription; Reader on its own resource group is the least that does.
resource "azurerm_role_assignment" "ado_login" {
  scope                = azurerm_resource_group.ado.id
  role_definition_name = "Reader"
  principal_id         = local.azure_identity.build.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "setup_login" {
  scope                = azurerm_resource_group.ado.id
  role_definition_name = "Reader"
  principal_id         = local.azure_identity.setup.principal_id
  principal_type       = "ServicePrincipal"
}

data "azurerm_subscription" "this" {}

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
  # Globally unique per ADO organization and subscription (setup_console/stacks.py state_account: same formula).
  name                            = "st${substr(lower(replace(var.ado_org, "/[^a-zA-Z0-9]/", "")), 0, 12)}${substr(lower(replace(var.azure_subscription_id, "-", "")), 0, 8)}"
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

# Whoever applies this stack (an admin, with their own sign-in, from the Infrastructure page) writes its own state into the container; an operator listed below already can.
data "azurerm_client_config" "current" {}

resource "azurerm_role_assignment" "runner_state" {
  count                = contains([for id in values(local.operator_ids) : id if id != null], data.azurerm_client_config.current.object_id) ? 0 : 1
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
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
