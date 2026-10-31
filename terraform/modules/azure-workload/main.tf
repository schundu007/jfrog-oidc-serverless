# One Azure serverless workload's own base: its resource group, runtime identity, and, as the
# hosting needs them, a registry, a Container Apps environment and a storage account. Every
# workload stack calls this with its own name, so no two workloads share a resource.

locals {
  # Global names (registry, storage) are derived from the workload and the subscription.
  suffix = substr(sha1("${var.azure_subscription_id}/${var.workload}"), 0, 6)
  alnum  = replace(var.workload, "-", "")
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${var.workload}"
  location = var.location
  tags     = var.tags
}

resource "azurerm_user_assigned_identity" "this" {
  name                = "id-${var.workload}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = var.tags
}

# The deployer (ADO build pipeline identity) rolls out the workload in its own resource group.
resource "azurerm_role_assignment" "deployer_contributor" {
  count                = var.deployer_principal_id == "" ? 0 : 1
  scope                = azurerm_resource_group.this.id
  role_definition_name = "Contributor"
  principal_id         = var.deployer_principal_id
  principal_type       = "ServicePrincipal"
}

# --- registry: images pulled with the workload's own identity --------------------------------

resource "azurerm_container_registry" "this" {
  count               = var.registry ? 1 : 0
  name                = substr("cr${local.alnum}${local.suffix}", 0, 50)
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = var.acr_sku
  admin_enabled       = false
  tags                = var.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  count                = var.registry ? 1 : 0
  scope                = azurerm_container_registry.this[0].id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deployer_acr_push" {
  count                = var.registry && var.deployer_principal_id != "" ? 1 : 0
  scope                = azurerm_container_registry.this[0].id
  role_definition_name = "AcrPush"
  principal_id         = var.deployer_principal_id
  principal_type       = "ServicePrincipal"
}

# --- Container Apps environment ----------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "this" {
  count               = var.container_apps_environment ? 1 : 0
  name                = "log-${var.workload}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_container_app_environment" "this" {
  count                      = var.container_apps_environment ? 1 : 0
  name                       = "cae-${var.workload}"
  location                   = azurerm_resource_group.this.location
  resource_group_name        = azurerm_resource_group.this.name
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this[0].id
  tags                       = var.tags
}

# --- storage: the Functions host's own account, reached with the identity (no keys) -----------

resource "azurerm_storage_account" "this" {
  count                           = var.storage ? 1 : 0
  name                            = substr("st${local.alnum}${local.suffix}", 0, 24)
  location                        = azurerm_resource_group.this.location
  resource_group_name             = azurerm_resource_group.this.name
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

resource "azurerm_role_assignment" "storage" {
  for_each             = var.storage ? toset(["Storage Blob Data Owner", "Storage Queue Data Contributor", "Storage Table Data Contributor"]) : toset([])
  scope                = azurerm_storage_account.this[0].id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  principal_type       = "ServicePrincipal"
}

# The deployer uploads code packages to the deployments container.
resource "azurerm_role_assignment" "deployer_blob" {
  count                = var.storage && var.deployer_principal_id != "" ? 1 : 0
  scope                = azurerm_storage_account.this[0].id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.deployer_principal_id
  principal_type       = "ServicePrincipal"
}
