output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "audience_app_id" {
  description = "Client ID of the audience app; the aud claim JFrog maps on."
  value       = azuread_application.jfrog_audience.client_id
}

output "audience_uri" {
  description = "Resource workloads request (JFROG_OIDC_AUDIENCE on Azure)."
  value       = "api://${azuread_application.jfrog_audience.client_id}"
}
