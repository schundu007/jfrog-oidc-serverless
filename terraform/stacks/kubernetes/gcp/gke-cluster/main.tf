# Test infrastructure only: a GKE cluster to try the kubelet credential provider on.
# In production the cluster already exists and only the Kubernetes console's
# configure step runs against it. Workload Identity gives the cluster its OIDC issuer.

resource "google_project_service" "container" {
  project            = var.gcp_project_id
  service            = "container.googleapis.com"
  disable_on_destroy = false
}

resource "google_container_cluster" "cluster" {
  name                = var.name
  location            = var.zone
  deletion_protection = false

  workload_identity_config {
    workload_pool = "${var.gcp_project_id}.svc.id.goog"
  }

  depends_on = [google_project_service.container]
}

resource "google_container_node_pool" "default" {
  name       = "default"
  cluster    = google_container_cluster.cluster.name
  location   = var.zone
  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}
