output "gcp_provider_name" {
  description = "Value workloads on GCP send as provider_name (JFROG_OIDC_PROVIDER)."
  value       = var.gcp == null ? null : platform_oidc_configuration.gcp[0].name
}

output "gcp_audience" {
  description = "Audience GCP workloads request from the metadata server (JFROG_OIDC_AUDIENCE)."
  value       = var.gcp == null ? null : var.gcp.audience
}

output "azure_provider_name" {
  description = "Value workloads on Azure send as provider_name (JFROG_OIDC_PROVIDER)."
  value       = var.azure == null ? null : platform_oidc_configuration.azure[0].name
}

output "mapping_usernames" {
  description = "Fixed username on each pull mapping's tokens (null for group-only runtime mappings)."
  value       = { for k, w in var.workloads : k => w.username }
}
