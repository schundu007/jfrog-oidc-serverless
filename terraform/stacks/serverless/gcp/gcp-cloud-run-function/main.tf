# GCP: a Cloud Run function (gen2) reads JFrog artifacts with a short-lived OIDC token. Its own
# service account and source bucket; nothing is shared with another workload.
#
#   apply 1 (deploy_function = false): the service account and the source bucket.
#   then:   the build pipeline (kind function) uploads apps/cloud-run-function to the bucket.
#   apply 2 (deploy_function = true, source_object): the function.
#
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (gcp-workload-onboard).

data "google_project" "this" {
  project_id = var.gcp_project_id
}

resource "google_service_account" "workload" {
  project      = var.gcp_project_id
  account_id   = var.workload
  display_name = "${var.workload} (Cloud Run function, JFrog OIDC)"
}

# The deployer (ADO build pipeline) rolls out the workload, which runs as this account.
resource "google_service_account_iam_member" "deployer_act_as" {
  count              = var.deployer_service_account == "" ? 0 : 1
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

# Source packages: <project>-<workload>-src, written by the build pipeline, read by the build.
resource "google_storage_bucket" "source" {
  project                     = var.gcp_project_id
  name                        = "${var.gcp_project_id}-${var.workload}-src"
  location                    = var.gcp_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "deployer_writes_source" {
  count  = var.deployer_service_account == "" ? 0 : 1
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.deployer_service_account}"
}

# Function builds run as the default compute account.
resource "google_storage_bucket_iam_member" "build_reads_source" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

resource "google_cloudfunctions2_function" "workload" {
  count    = var.deploy_function ? 1 : 0
  project  = var.gcp_project_id
  name     = var.workload
  location = var.gcp_region

  build_config {
    runtime     = "python312"
    entry_point = "handler"
    source {
      storage_source {
        bucket = google_storage_bucket.source.name
        object = var.source_object
      }
    }
  }

  service_config {
    service_account_email = google_service_account.workload.email
    min_instance_count    = 0
    max_instance_count    = 2
    available_memory      = "256M"
  }

  lifecycle {
    # The build pipeline owns the source; the console's wire step owns the identity and JFROG_*.
    ignore_changes = [build_config[0].source, build_config[0].docker_repository, service_config[0].environment_variables, service_config[0].service_account_email]
    precondition {
      condition     = var.source_object != ""
      error_message = "Set source_object (uploaded by the build pipeline) before turning deploy_function on."
    }
  }

  depends_on = [google_storage_bucket_iam_member.build_reads_source]
}
