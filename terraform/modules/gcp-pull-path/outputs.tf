output "rotator_service_account_email" {
  value = google_service_account.rotator.email
}

output "rotator_unique_id" {
  description = "sub claim of the rotator's Google ID token; use as the JFrog mapping subject."
  value       = google_service_account.rotator.unique_id
}

output "upstream_secret_id" {
  value = google_secret_manager_secret.upstream.id
}

output "repository_urls" {
  description = "Base URLs to use in images, .npmrc, requirements.txt and settings.xml."
  value = {
    for k, r in google_artifact_registry_repository.remote :
    k => (
      r.format == "PYTHON"
      ? "https://${var.region}-python.pkg.dev/${var.project_id}/${k}/simple/"
      : r.format == "DOCKER"
      ? "${var.region}-docker.pkg.dev/${var.project_id}/${k}"
      : "https://${var.region}-${local.ar_hosts[r.format]}.pkg.dev/${var.project_id}/${k}/"
    )
  }
}

output "rotator_job_name" {
  value = google_cloud_run_v2_job.rotator.name
}

output "first_run_command" {
  value = "gcloud run jobs execute ${google_cloud_run_v2_job.rotator.name} --project ${var.project_id} --region ${var.region} --wait"
}
