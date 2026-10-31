output "workload_service_account" {
  value = google_service_account.workload.email
}

output "source_bucket" {
  value = google_storage_bucket.source.name
}

output "function_name" {
  value = var.deploy_function ? google_cloudfunctions2_function.workload[0].name : null
}

output "function_url" {
  value = var.deploy_function ? google_cloudfunctions2_function.workload[0].url : null
}
