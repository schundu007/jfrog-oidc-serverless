# One-time trust for Azure DevOps pipelines (apply once, as an admin, locally).
# After this, setup (Terraform), build and deploy run in ADO with no stored
# secrets: each job gets its service connection's Entra-issued OIDC token
# (iss https://login.microsoftonline.com/<tenant>/v2.0, aud
# api://AzureADTokenExchange, sub unique per connection) and exchanges it:
#
#   JFrog  -> integration ado-<org>, one mapping per connection
#             build: reader group (read + remote cache), setup: admin
#   GCP    -> Workload Identity pool "azure-devops", one service account per connection
#   Azure  -> the connection's managed identity (federated credential)

locals {
  issuer = "https://login.microsoftonline.com/${var.azure_tenant_id}/v2.0"
  # Entra federated credentials always use api://AzureADTokenExchange; the token
  # a job requests from System.OidcRequestUri carries var.ado_token_audience.
  audience = "api://AzureADTokenExchange"
  pool     = "projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.ado.workload_identity_pool_id}"
}

# --- JFrog ---------------------------------------------------------------------------

resource "platform_oidc_configuration" "ado" {
  name          = "ado-${var.ado_org}"
  description   = "Azure DevOps ${var.ado_org} pipelines (Entra-issued service connection tokens)"
  issuer_url    = local.issuer
  provider_type = "generic"
  audience      = var.ado_token_audience
}

resource "platform_oidc_identity_mapping" "build" {
  name          = "ado-build"
  description   = "ADO build/deploy pipelines (${var.connections.build.name})"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 1
  claims_json   = jsonencode({ sub = var.connections.build.subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/groups:\"${var.jfrog_reader_group}\""
    audience   = "jfrt@*"
    expires_in = 3600
  }
}

resource "platform_oidc_identity_mapping" "setup" {
  name          = "ado-setup"
  description   = "ADO setup pipelines: Terraform admin (${var.connections.setup.name})"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 2
  claims_json   = jsonencode({ sub = var.connections.setup.subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/admin"
    username   = "ado-setup"
    audience   = "jfrt@* jfac@*"
    expires_in = 3600
  }
}

# --- Azure: the connections' managed identity -----------------------------------------

data "azurerm_user_assigned_identity" "ado" {
  name                = var.azure_identity.name
  resource_group_name = var.azure_identity.resource_group
}

resource "azurerm_federated_identity_credential" "ado" {
  for_each = var.connections

  name                      = "ado-${each.value.name}"
  user_assigned_identity_id = data.azurerm_user_assigned_identity.ado.id
  issuer                    = local.issuer
  subject                   = each.value.subject
  audience                  = [local.audience]
}

# AzureCLI@2 logs in before any script runs, so the identity must see the
# subscription; Reader on its own resource group is the least that does.
data "azurerm_resource_group" "ado" {
  name = var.azure_identity.resource_group
}

resource "azurerm_role_assignment" "ado_login" {
  scope                = data.azurerm_resource_group.ado.id
  role_definition_name = "Reader"
  principal_id         = data.azurerm_user_assigned_identity.ado.principal_id
  principal_type       = "ServicePrincipal"
}

# --- GCP: Workload Identity Federation ------------------------------------------------

data "google_project" "this" {
  project_id = var.gcp_project_id
}

resource "google_iam_workload_identity_pool" "ado" {
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
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition                = "assertion.sub in [${join(", ", [for c in var.connections : "'${c.subject}'"])}]"
  oidc {
    issuer_uri        = local.issuer
    allowed_audiences = [var.ado_token_audience]
  }
}

resource "google_service_account" "ado" {
  for_each = var.connections

  project      = var.gcp_project_id
  account_id   = "ado-${each.key}"
  display_name = "ADO ${each.value.name} (${each.key})"
}

resource "google_service_account_iam_member" "ado_wif" {
  for_each = var.connections

  service_account_id = google_service_account.ado[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${local.pool}/subject/${each.value.subject}"
}

# build: push images, roll out the job image and run it. actAs on each workload's
# service account is granted by that workload's stack (deployer_service_account).
resource "google_artifact_registry_repository_iam_member" "build_push" {
  project    = var.gcp_project_id
  location   = var.gcp_region
  repository = var.gcp_image_repository
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ado["build"].email}"
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/run.developer", "roles/logging.viewer"])

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
  ])

  project = var.gcp_project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.ado["setup"].email}"
}

resource "google_storage_bucket" "tfstate" {
  project                     = var.gcp_project_id
  name                        = "${var.gcp_project_id}-tfstate"
  location                    = upper(var.gcp_region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
}

resource "google_storage_bucket_iam_member" "setup_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ado["setup"].email}"
}
