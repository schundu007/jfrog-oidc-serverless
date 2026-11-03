# GCP: a Cloud Run service reads JFrog artifacts with a short-lived OIDC token. Its own service
# account; nothing is shared with another workload. Images come from Artifact Registry.
#
#   apply 1 (deploy_service = false): the service account.
#   then:   the build pipeline (kind service) pushes apps/cloud-run-service.
#   apply 2 (deploy_service = true, service_image): the Cloud Run service.
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
  display_name = "${var.workload} (Cloud Run service, JFrog OIDC)"
}

# The deployer (ADO build pipeline) rolls out the workload, which runs as this account.
resource "google_service_account_iam_member" "deployer_act_as" {
  count              = var.deployer_service_account == "" ? 0 : 1
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

resource "google_cloud_run_v2_service" "workload" {
  count               = var.deploy_service ? 1 : 0
  project             = var.gcp_project_id
  name                = var.workload
  location            = var.gcp_region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.workload.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.service_image
    }
  }

  lifecycle {
    # The build pipeline owns the image tag; the console's wire step owns JFROG_*.
    ignore_changes = [template[0].containers[0].image, template[0].containers[0].env, client, client_version]
    precondition {
      condition     = var.service_image != ""
      error_message = "Set service_image (pushed by the build pipeline) before turning deploy_service on."
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.deploy_service && var.public ? 1 : 0
  project  = var.gcp_project_id
  location = var.gcp_region
  name     = google_cloud_run_v2_service.workload[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
