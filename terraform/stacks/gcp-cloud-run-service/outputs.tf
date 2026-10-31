output "workload_service_account" {
  value = google_service_account.workload.email
}

output "image_repository" {
  value = "${var.gcp_region}-docker.pkg.dev/${var.gcp_project_id}/${data.google_artifact_registry_repository.images.repository_id}"
}

output "service_name" {
  value = var.deploy_service ? google_cloud_run_v2_service.workload[0].name : null
}

output "service_url" {
  value = var.deploy_service ? google_cloud_run_v2_service.workload[0].uri : null
}
