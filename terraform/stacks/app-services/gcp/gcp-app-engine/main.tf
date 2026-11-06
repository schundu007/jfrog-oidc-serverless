# GCP: an App Engine standard service reads JFrog artifacts with a short-lived OIDC token. The
# service runs as its own service account (never the default App Engine account); nothing is
# shared with another workload. No registry: the build pipeline deploys source, with its Python
# packages vendored from JFrog.
#
#   apply:  the service account (and, with create_application, the project's App Engine application).
#   then:   the build pipeline (kind app-engine) deploys apps/cloud-run-service as service <workload>.
#
# Its JFrog mapping belongs to the console's workspace (gcp-workload-onboard). App Engine variables
# change only with a deploy, so build-deploy writes JFROG_* into app.yaml.

resource "google_service_account" "workload" {
  project      = var.gcp_project_id
  account_id   = var.workload
  display_name = "${var.workload} (App Engine service, JFrog OIDC)"
}

# The deployer (ADO build pipeline) deploys versions that run as this account.
resource "google_service_account_iam_member" "deployer_act_as" {
  count              = var.deployer_service_account == "" ? 0 : 1
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deployer_service_account}"
}

# One per project, region fixed for life, and Google offers no delete: destroy only forgets it.
resource "google_app_engine_application" "app" {
  count       = var.create_application ? 1 : 0
  project     = var.gcp_project_id
  location_id = var.location_id != "" ? var.location_id : var.gcp_region

  lifecycle {
    ignore_changes = [location_id]
  }
}
