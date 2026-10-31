output "azure_setup_identity_client_id" {
  description = "Set as the setup service connection's identity (servicePrincipalId)."
  value       = azurerm_user_assigned_identity.setup.client_id
}

output "azure_tfstate" {
  value = { storage_account = azurerm_storage_account.tfstate.name, container = azurerm_storage_container.tfstate.name, resource_group = data.azurerm_resource_group.ado.name }
}

output "azure_setup_identity_principal_id" {
  description = "The setup pipelines' identity (object ID), for the console's Preflight checks."
  value       = azurerm_user_assigned_identity.setup.principal_id
}

output "jfrog_provider_name" {
  value = platform_oidc_configuration.ado.name
}
