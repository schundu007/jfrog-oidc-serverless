output "jfrog_provider_name" {
  value = platform_oidc_configuration.ado.name
}

output "gcp_workload_identity_provider" {
  value = "${local.pool}/providers/${google_iam_workload_identity_pool_provider.ado.workload_identity_pool_provider_id}"
}

output "gcp_service_accounts" {
  value = { for k, sa in google_service_account.ado : k => sa.email }
}

output "tfstate_bucket" {
  value = google_storage_bucket.tfstate.name
}

output "azure_setup_identity_client_id" {
  description = "Set as the setup service connection's identity (servicePrincipalId)."
  value       = azurerm_user_assigned_identity.setup.client_id
}

output "azure_tfstate" {
  value = { storage_account = azurerm_storage_account.tfstate.name, container = azurerm_storage_container.tfstate.name, resource_group = data.azurerm_resource_group.ado.name }
}
