# One Azure workload's own base: its runtime identity and, as the hosting needs them, a registry, a
# Container Apps environment and a storage account, in the workload's resource group. Every workload
# stack calls this with its own name, so no two workloads share a resource.
#
# No role assignments here: the pipelines' identities cannot grant roles. The resource group, the pipeline
# identities' roles on it and the runtime identity's roles (runtime_roles: AcrPull, Functions host storage)
# all come from azure-pipeline-identity, which an admin applies with their own sign-in.

locals {
  user_assigned = var.identity_type == "UserAssigned"
  # Global names (registry, storage) are derived from the workload and the subscription.
  suffix = substr(sha1("${var.azure_subscription_id}/${var.workload}"), 0, 6)
  alnum  = replace(var.workload, "-", "")
}

# Made by azure-pipeline-identity (workloads), with the setup and build identities' roles on it.
data "azurerm_resource_group" "this" {
  name = "rg-${var.workload}"
}

# Only with identity_type = UserAssigned: made by azure-pipeline-identity with its roles, so it can pull
# from the registry before the workload's first start. SystemAssigned: the platform creates the identity
# with the workload and deletes it with the workload; no other resource can use it.
data "azurerm_user_assigned_identity" "this" {
  count               = local.user_assigned ? 1 : 0
  name                = "id-${var.workload}"
  resource_group_name = data.azurerm_resource_group.this.name
}

# --- registry: images pulled with the workload's own identity --------------------------------

resource "azurerm_container_registry" "this" {
  count               = var.registry ? 1 : 0
  name                = coalesce(var.acr_name, substr("cr${local.alnum}${local.suffix}", 0, 50))
  location            = data.azurerm_resource_group.this.location
  resource_group_name = data.azurerm_resource_group.this.name
  sku                 = var.acr_sku
  admin_enabled       = false
  tags                = var.tags
}

# --- Container Apps environment ----------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "this" {
  count               = var.container_apps_environment ? 1 : 0
  name                = "log-${var.workload}"
  location            = data.azurerm_resource_group.this.location
  resource_group_name = data.azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_container_app_environment" "this" {
  count                      = var.container_apps_environment ? 1 : 0
  name                       = "cae-${var.workload}"
  location                   = data.azurerm_resource_group.this.location
  resource_group_name        = data.azurerm_resource_group.this.name
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this[0].id
  tags                       = var.tags
}

# --- storage: the Functions host's own account, reached with the identity (no keys) -----------

resource "azurerm_storage_account" "this" {
  count                           = var.storage ? 1 : 0
  name                            = substr("st${local.alnum}${local.suffix}", 0, 24)
  location                        = data.azurerm_resource_group.this.location
  resource_group_name             = data.azurerm_resource_group.this.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  tags                            = var.tags
}

resource "azurerm_storage_container" "deployments" {
  count                 = var.storage ? 1 : 0
  name                  = "deployments"
  storage_account_id    = azurerm_storage_account.this[0].id
  container_access_type = "private"
}

locals {
  storage_roles = ["Storage Blob Data Owner", "Storage Queue Data Contributor", "Storage Table Data Contributor"]
  # What the runtime identity needs; azure-pipeline-identity grants these (workloads.runtime_principal_id).
  runtime_roles = concat(
    var.registry ? [{ role = "AcrPull", scope = azurerm_container_registry.this[0].id }] : [],
    var.storage ? [for r in local.storage_roles : { role = r, scope = azurerm_storage_account.this[0].id }] : [],
  )
}
