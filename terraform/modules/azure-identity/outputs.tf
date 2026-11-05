output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "audience_app_id" {
  description = "Client ID of the audience app; the aud claim JFrog maps on."
  value       = local.app_registration ? azuread_application.jfrog_audience[0].client_id : local.token_exchange_app_id
}

output "audience_uri" {
  description = "Resource workloads request (JFROG_OIDC_AUDIENCE on Azure)."
  value       = local.app_registration ? "api://${azuread_application.jfrog_audience[0].client_id}" : "api://AzureADTokenExchange"
}
