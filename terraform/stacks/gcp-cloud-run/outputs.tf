output "demo_service_account" {
  value = google_service_account.demo.email
}

output "runtime_demo_command" {
  value = var.deploy_demo ? "gcloud run jobs execute ${google_cloud_run_v2_job.runtime_demo[0].name} --project ${var.gcp_project_id} --region ${var.gcp_region} --wait" : null
}
