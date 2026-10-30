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

output "aws_provider_name" {
  description = "Value AWS workloads send as provider_name (JFROG_OIDC_PROVIDER)."
  value       = var.aws == null ? null : platform_oidc_configuration.aws[0].name
}

output "aws_audience" {
  description = "Audience AWS workloads request from STS GetWebIdentityToken (JFROG_OIDC_AUDIENCE)."
  value       = var.aws == null ? null : var.aws.audience
}

output "openshift_provider_name" {
  description = "Value Knative workloads send as provider_name (JFROG_OIDC_PROVIDER)."
  value       = var.openshift == null ? null : platform_oidc_configuration.openshift[0].name
}

output "openshift_audience" {
  description = "Audience of the projected service-account token (JFROG_OIDC_AUDIENCE)."
  value       = var.openshift == null ? null : var.openshift.audience
}

output "mapping_usernames" {
  description = "Fixed username on each pull mapping's tokens (null for group-only runtime mappings)."
  value       = { for k, w in var.workloads : k => w.username }
}
