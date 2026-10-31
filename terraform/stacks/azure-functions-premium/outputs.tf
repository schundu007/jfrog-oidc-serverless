output "resource_group" {
  value = module.base.resource_group_name
}

output "workload_identity_client_id" {
  value = module.base.identity.client_id
}

output "function_name" {
  value = azurerm_linux_function_app.workload.name
}

output "function_url" {
  value = "https://${azurerm_linux_function_app.workload.default_hostname}"
}
