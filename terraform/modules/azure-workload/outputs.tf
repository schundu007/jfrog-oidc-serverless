output "resource_group_name" {
  value = data.azurerm_resource_group.this.name
}

output "resource_group_id" {
  value = data.azurerm_resource_group.this.id
}

output "location" {
  value = data.azurerm_resource_group.this.location
}

output "identity" {
  description = "The user-assigned identity; every field is null with SystemAssigned."
  value = {
    type         = var.identity_type
    id           = local.user_assigned ? data.azurerm_user_assigned_identity.this[0].id : null
    client_id    = local.user_assigned ? data.azurerm_user_assigned_identity.this[0].client_id : null
    principal_id = local.user_assigned ? data.azurerm_user_assigned_identity.this[0].principal_id : null
  }
}

output "runtime_roles" {
  description = "Roles the runtime identity needs, as { role, scope }."
  value       = local.runtime_roles
}

output "acr_login_server" {
  value = var.registry ? azurerm_container_registry.this[0].login_server : null
}

output "container_app_environment_id" {
  value = var.container_apps_environment ? azurerm_container_app_environment.this[0].id : null
}

output "storage_account_name" {
  value = var.storage ? azurerm_storage_account.this[0].name : null
}

output "deployments_container_endpoint" {
  value = var.storage ? "${azurerm_storage_account.this[0].primary_blob_endpoint}${azurerm_storage_container.deployments[0].name}" : null
}

