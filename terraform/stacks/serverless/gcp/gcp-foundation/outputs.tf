output "gcp_workload_identity_provider" {
  value = "${local.pool}/providers/${google_iam_workload_identity_pool_provider.ado.workload_identity_pool_provider_id}"
}

output "gcp_service_accounts" {
  value = { for k, sa in google_service_account.ado : k => sa.email }
}

output "tfstate_bucket" {
  value = google_storage_bucket.tfstate.name
}

output "jfrog_provider_name" {
  value = platform_oidc_configuration.ado.name
}
