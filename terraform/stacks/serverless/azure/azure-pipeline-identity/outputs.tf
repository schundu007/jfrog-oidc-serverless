output "azure_setup_identity_client_id" {
  description = "Set as the setup service connection's identity (servicePrincipalId)."
  value       = azurerm_user_assigned_identity.setup.client_id
}

output "azure_tfstate" {
  value = { storage_account = azurerm_storage_account.tfstate.name, container = azurerm_storage_container.tfstate.name, resource_group = azurerm_resource_group.ado.name }
}

output "azure_setup_identity_principal_id" {
  description = "The setup pipelines' identity (object ID), for the console's Preflight checks."
  value       = azurerm_user_assigned_identity.setup.principal_id
}

output "argocd_identity" {
  description = "Argo CD's account (ADO Basic, project Readers): set as aks-cluster's argocd_identity."
  value       = { name = azurerm_user_assigned_identity.argocd.name, resource_group = azurerm_resource_group.ado.name }
}

output "argocd_identity_client_id" {
  value = azurerm_user_assigned_identity.argocd.client_id
}
