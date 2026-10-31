output "workload_service_account" {
  value = google_service_account.workload.email
}

output "image_repository" {
  value = "${var.gcp_region}-docker.pkg.dev/${var.gcp_project_id}/${data.google_artifact_registry_repository.images.repository_id}"
}

output "worker_pool_name" {
  value = var.deploy_worker_pool ? google_cloud_run_v2_worker_pool.workload[0].name : null
}
