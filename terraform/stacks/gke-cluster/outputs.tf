output "cluster_name" {
  value = google_container_cluster.cluster.name
}

output "location" {
  value = google_container_cluster.cluster.location
}

output "oidc_issuer_url" {
  value = "https://container.googleapis.com/v1/projects/${var.gcp_project_id}/locations/${var.zone}/clusters/${google_container_cluster.cluster.name}"
}

output "kubeconfig_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.cluster.name} --zone ${var.zone} --project ${var.gcp_project_id}"
}
