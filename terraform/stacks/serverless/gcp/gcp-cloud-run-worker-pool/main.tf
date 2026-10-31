# GCP: a Cloud Run worker pool (always-on, no HTTP endpoint: queue consumers, pull workers) reads
# JFrog artifacts with a short-lived OIDC token. Its own service account; nothing is shared with
# another workload. Images come from Artifact Registry.
#
#   apply 1 (deploy_worker_pool = false): the service account.
#   then:   the build pipeline (kind worker-pool) pushes apps/cloud-run-worker-pool.
#   apply 2 (deploy_worker_pool = true, worker_pool_image): the Cloud Run worker pool.
#
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (gcp-workload-onboard).

data "google_artifact_registry_repository" "images" {
  project       = var.gcp_project_id
  location      = var.gcp_region
  repository_id = var.image_repository
}

resource "google_service_account" "workload" {
  project      = var.gcp_project_id
  account_id   = var.workload
  display_name = "${var.workload} (Cloud Run worker pool, JFrog OIDC)"
}

# The deployer (ADO build pipeline) rolls out the workload, which runs as this account.
resource "google_service_account_iam_member" "deployer_act_as" {
  count              = var.deployer_service_account == "" ? 0 : 1
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

resource "google_cloud_run_v2_worker_pool" "workload" {
  count               = var.deploy_worker_pool ? 1 : 0
  project             = var.gcp_project_id
  name                = var.workload
  location            = var.gcp_region
  deletion_protection = false

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.instances
  }

  template {
    service_account = google_service_account.workload.email
    containers {
      image = var.worker_pool_image
    }
  }

  lifecycle {
    # The build pipeline owns the image tag; the console's wire step owns JFROG_*.
    ignore_changes = [template[0].containers[0].image, template[0].containers[0].env, client, client_version]
    precondition {
      condition     = var.worker_pool_image != ""
      error_message = "Set worker_pool_image (pushed by the build pipeline) before turning deploy_worker_pool on."
    }
  }
}
