output "workload_service_account" {
  value = google_service_account.workload.email
}

output "image_repository" {
  value = "${var.gcp_region}-docker.pkg.dev/${var.gcp_project_id}/${data.google_artifact_registry_repository.images.repository_id}"
}

output "job_name" {
  value = var.deploy_job ? google_cloud_run_v2_job.workload[0].name : null
}
