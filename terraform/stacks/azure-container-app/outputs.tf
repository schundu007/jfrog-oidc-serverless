output "resource_group" {
  value = module.base.resource_group_name
}

output "acr_login_server" {
  value = module.base.acr_login_server
}

output "workload_identity_client_id" {
  value = module.base.identity.client_id
}

output "app_name" {
  value = var.deploy_app ? azurerm_container_app.workload[0].name : null
}

output "app_url" {
  value = var.deploy_app ? "https://${azurerm_container_app.workload[0].ingress[0].fqdn}" : null
}
