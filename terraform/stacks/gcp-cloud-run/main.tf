# GCP: a Cloud Run job reads JFrog artifacts with a short-lived OIDC token.
# Images come from Artifact Registry and are pulled by Cloud Run with its own
# identity; JFrog holds the artifacts. No stored credentials anywhere.
#
# Run by the ADO setup pipeline (pipelines/gcp-setup.yml). The build pipeline
# (pipelines/gcp-build-deploy.yml) pushes the image and rolls it out, so the job
# image is ignored here after creation.
#
#   apply 1 (deploy_job = false): the image repository, the service account.
#   then:   the build pipeline pushes apps/artifact-reader.
#   apply 2 (deploy_job = true): the Cloud Run job.
#
# The JFrog integration and mapping, and the job's JFROG_* settings, belong to
# the console's workspace (terraform/workspaces/gcp_<project>_<region>,
# gcp-workload-onboard).

# Moved to the console's workspace: the integration stays in JFrog (that
# workspace adopted it); the stack's duplicate mapping is destroyed.
removed {
  from = module.jfrog_oidc.platform_oidc_configuration.gcp
  lifecycle {
    destroy = false
  }
}

# Created by ADO trust (the build pipeline pushes to it before this stack deploys a job).
data "google_artifact_registry_repository" "images" {
  project       = var.gcp_project_id
  location      = var.gcp_region
  repository_id = var.image_repository
}

resource "google_service_account" "workload" {
  project      = var.gcp_project_id
  account_id   = var.workload
  display_name = "${var.workload} (Cloud Run, JFrog OIDC)"
}

# The deployer (ADO build pipeline) rolls out the job, which runs as this account.
resource "google_service_account_iam_member" "deployer_act_as" {
  count              = var.deployer_service_account == "" ? 0 : 1
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

resource "google_cloud_run_v2_job" "workload" {
  count               = var.deploy_job ? 1 : 0
  project             = var.gcp_project_id
  name                = var.workload
  location            = var.gcp_region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.workload.email
      max_retries     = 0
      containers {
        image = var.job_image
        env {
          name  = "JFROG_ARTIFACT_PATH"
          value = var.artifact_path
        }
      }
    }
  }

  lifecycle {
    # The build pipeline owns the image tag; the console's wire step owns JFROG_*.
    ignore_changes = [template[0].template[0].containers[0].image, template[0].template[0].containers[0].env]
  }
}
