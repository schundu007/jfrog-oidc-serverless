output "acr_login_server" {
  value = azurerm_container_registry.images.login_server
}

output "workload_identity_client_id" {
  value = azurerm_user_assigned_identity.workload.client_id
}

output "resource_group" {
  value = azurerm_resource_group.workload.name
}

output "job_name" {
  value = var.deploy_job ? azurerm_container_app_job.workload[0].name : null
}
