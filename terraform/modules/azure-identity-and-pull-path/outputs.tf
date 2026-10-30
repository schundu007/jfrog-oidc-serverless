output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "audience_app_id" {
  description = "Client ID of the audience app; the aud claim JFrog maps on."
  value       = azuread_application.jfrog_audience.client_id
}

output "audience_uri" {
  description = "Resource workloads request (JFROG_OIDC_AUDIENCE on Azure)."
  value       = local.audience_uri
}

output "rotator_principal_id" {
  description = "oid claim of the rotator's token; use as the JFrog image-pull mapping subject."
  value       = one(azurerm_user_assigned_identity.rotator[*].principal_id)
}

output "registry_username" {
  value = var.registry_username
}

output "registry_secret_versionless_url" {
  description = "Versionless Key Vault URL. Use it in Container Apps secrets and @Microsoft.KeyVault(SecretUri=...) references so rotation is picked up."
  value       = one(azurerm_key_vault_secret.registry_token[*].versionless_id)
}

output "rotator_job_name" {
  value = one(azurerm_container_app_job.rotator[*].name)
}

output "first_run_command" {
  value = var.pull_path_enabled ? "az containerapp job start --name ${azurerm_container_app_job.rotator[0].name} --resource-group ${var.resource_group_name}" : null
}
