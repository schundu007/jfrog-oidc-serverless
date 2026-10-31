output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "resource_group_id" {
  value = azurerm_resource_group.this.id
}

output "location" {
  value = azurerm_resource_group.this.location
}

output "identity" {
  value = {
    id           = azurerm_user_assigned_identity.this.id
    client_id    = azurerm_user_assigned_identity.this.client_id
    principal_id = azurerm_user_assigned_identity.this.principal_id
  }
}

output "acr_login_server" {
  value = var.registry ? azurerm_container_registry.this[0].login_server : null
}

output "acr_pull_ready" {
  description = "Depend on this before a workload pulls from the registry."
  value       = var.registry ? azurerm_role_assignment.acr_pull[0].id : null
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

output "storage_ready" {
  description = "Depend on this before a Functions host starts."
  value       = [for r in azurerm_role_assignment.storage : r.id]
}
