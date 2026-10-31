# Google Cloud foundation for the ADO pipelines (apply once, locally): Workload Identity
# Federation for the ADO service connections, their service accounts, the image repository
# and the Terraform state bucket of the GCP stacks. No Azure or AWS resources here.

locals {
  issuer = "https://login.microsoftonline.com/${var.ado_tenant_id}/v2.0"
  pool   = "projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.ado.workload_identity_pool_id}"
}


data "google_project" "this" {
  project_id = var.gcp_project_id
}

# A new project has none of these on; the trust below, the stacks and the pipelines need them.
resource "google_project_service" "apis" {
  for_each = toset([
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
  ])

  project            = var.gcp_project_id
  service            = each.value
  disable_on_destroy = false
}

# The image repository the build pipeline pushes to; the gcp-cloud-run stack reads it.
resource "google_artifact_registry_repository" "images" {
  project       = var.gcp_project_id
  location      = var.gcp_region
  repository_id = var.gcp_image_repository
  format        = "DOCKER"
  description   = "Workload images (not proxied through JFrog)"

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool" "ado" {
  depends_on = [google_project_service.apis]

  project                   = var.gcp_project_id
  workload_identity_pool_id = "azure-devops"
  display_name              = "Azure DevOps"
  description               = "ADO ${var.ado_org} pipelines (no keys)"
}

resource "google_iam_workload_identity_pool_provider" "ado" {
  project                            = var.gcp_project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.ado.workload_identity_pool_id
  workload_identity_pool_provider_id = var.ado_org
  display_name                       = "ADO ${var.ado_org}"
  # google.subject is capped at 127 bytes; keep "<org-id>/<connection-id>".
  attribute_mapping   = { "google.subject" = "assertion.sub.extract('/sc/{id}')" }
  attribute_condition = "assertion.sub in [${join(", ", [for c in local.connections : "'${c.subject}'"])}]"
  oidc {
    issuer_uri        = local.issuer
    allowed_audiences = [var.ado_token_audience]
  }
}

resource "google_service_account" "ado" {
  depends_on = [google_project_service.apis]

  for_each = local.connections

  project      = var.gcp_project_id
  account_id   = "ado-${each.key}"
  display_name = "ADO ${each.value.name} (${each.key})"
}

resource "google_service_account_iam_member" "ado_wif" {
  for_each = local.connections

  service_account_id = google_service_account.ado[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${local.pool}/subject/${element(split("/sc/", each.value.subject), 1)}"
}

# build: push images, roll out jobs and services, deploy functions from source.
# Service and function deploys act as the app's runtime account, which the console
# may have created (jfrog-<name>), so actAs is project-wide in this test project.
resource "google_artifact_registry_repository_iam_member" "build_push" {
  project    = var.gcp_project_id
  location   = var.gcp_region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ado["build"].email}"
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/run.developer", "roles/logging.viewer", "roles/cloudfunctions.developer",
  "roles/iam.serviceAccountUser"])

  project = var.gcp_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.ado["build"].email}"
}

# setup: what the gcp-cloud-run workspace manages, plus its state.
resource "google_project_iam_member" "setup" {
  for_each = toset([
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/artifactregistry.admin",
    "roles/run.admin",
    "roles/container.admin",                # gke-cluster stack
    "roles/serviceusage.serviceUsageAdmin", # enables container.googleapis.com
  ])

  project = var.gcp_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.ado["setup"].email}"
}

resource "google_storage_bucket" "tfstate" {
  depends_on = [google_project_service.apis]

  project                     = var.gcp_project_id
  name                        = "${var.gcp_project_id}-tfstate"
  location                    = upper(var.gcp_region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # test infrastructure: destroy empties the state bucket
  versioning {
    enabled = true
  }
}

resource "google_storage_bucket_iam_member" "setup_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ado["setup"].email}"
}
