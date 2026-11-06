output "workload_service_account" {
  value = google_service_account.workload.email
}

output "workload_service_account_unique_id" {
  value = google_service_account.workload.unique_id
}

output "service_name" {
  value = var.workload
}

output "service_url" {
  value = var.create_application ? "https://${var.workload}-dot-${google_app_engine_application.app[0].default_hostname}" : null
}
