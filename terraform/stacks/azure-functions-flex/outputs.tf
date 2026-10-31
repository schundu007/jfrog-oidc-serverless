output "resource_group" {
  value = module.base.resource_group_name
}

output "workload_identity_client_id" {
  value = module.base.identity.client_id
}

output "function_name" {
  value = azurerm_function_app_flex_consumption.workload.name
}

output "function_url" {
  value = "https://${azurerm_function_app_flex_consumption.workload.default_hostname}"
}
