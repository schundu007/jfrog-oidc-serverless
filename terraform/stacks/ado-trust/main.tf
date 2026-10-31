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

# --- Azure: one managed identity per connection ----------------------------------------
#
# build (existing identity): rights come from each workload stack (AcrPush,
#   Contributor on the workload's resource group).
# setup: creates workload stacks. Contributor on the subscription, role
#   assignments limited to AcrPull/AcrPush/Contributor, Entra apps it owns.

data "azurerm_resource_group" "ado" {
  name = var.azure_identity.resource_group
}

data "azurerm_user_assigned_identity" "ado" {
  name                = var.azure_identity.name
  resource_group_name = var.azure_identity.resource_group
}

resource "azurerm_user_assigned_identity" "setup" {
  name                = "${var.azure_identity.name}-setup"
  location            = data.azurerm_resource_group.ado.location
  resource_group_name = data.azurerm_resource_group.ado.name
}

locals {
  azure_identity = {
    build = { id = data.azurerm_user_assigned_identity.ado.id, principal_id = data.azurerm_user_assigned_identity.ado.principal_id }
    setup = { id = azurerm_user_assigned_identity.setup.id, principal_id = azurerm_user_assigned_identity.setup.principal_id }
  }
  # AcrPull, AcrPush, Contributor
  assignable_roles = "7f951dda-4ed3-4680-a7ca-43fe172d538d, 8311e382-0749-4cb8-b61a-304f252e45ec, b24988ac-6180-42a0-ab88-20f7382dd24c"
}

resource "azurerm_federated_identity_credential" "ado" {
  for_each = var.connections

  name                      = "ado-${each.value.name}"
  user_assigned_identity_id = local.azure_identity[each.key].id
  issuer                    = local.issuer
  subject                   = each.value.subject
  audience                  = [local.audience]
}

# AzureCLI@2 logs in before any script runs, so the build identity must see the
# subscription; Reader on its own resource group is the least that does.
resource "azurerm_role_assignment" "ado_login" {
  scope                = data.azurerm_resource_group.ado.id
  role_definition_name = "Reader"
  principal_id         = local.azure_identity.build.principal_id
  principal_type       = "ServicePrincipal"
}

data "azurerm_subscription" "this" {}

resource "azurerm_role_assignment" "setup_contributor" {
  scope                = data.azurerm_subscription.this.id
  role_definition_name = "Contributor"
  principal_id         = local.azure_identity.setup.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "setup_rbac" {
  scope                = data.azurerm_subscription.this.id
  role_definition_name = "Role Based Access Control Administrator"
  principal_id         = local.azure_identity.setup.principal_id
  principal_type       = "ServicePrincipal"
  condition_version    = "2.0"
  condition            = <<-EOT
    ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${local.assignable_roles}}))
    AND
    ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${local.assignable_roles}}))
  EOT
}

# Entra: create and manage only the apps it owns (the workloads' audience apps).
data "azuread_service_principal" "graph" {
  client_id = "00000003-0000-0000-c000-000000000000"
}

resource "azuread_app_role_assignment" "setup_apps" {
  app_role_id         = data.azuread_service_principal.graph.app_role_ids["Application.ReadWrite.OwnedBy"]
  principal_object_id = local.azure_identity.setup.principal_id
  resource_object_id  = data.azuread_service_principal.graph.object_id
}

# Terraform state for Azure stacks: Entra auth only, versioned.
resource "azurerm_storage_account" "tfstate" {
  name                            = substr("st${replace(var.ado_org, "-", "")}tfstate", 0, 24)
  resource_group_name             = data.azurerm_resource_group.ado.name
  location                        = data.azurerm_resource_group.ado.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  default_to_oauth_authentication = true
  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}

resource "azurerm_role_assignment" "setup_state" {
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = local.azure_identity.setup.principal_id
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
  # google.subject is capped at 127 bytes; keep "<org-id>/<connection-id>".
  attribute_mapping   = { "google.subject" = "assertion.sub.extract('/sc/{id}')" }
  attribute_condition = "assertion.sub in [${join(", ", [for c in var.connections : "'${c.subject}'"])}]"
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
  member             = "principal://iam.googleapis.com/${local.pool}/subject/${element(split("/sc/", each.value.subject), 1)}"
}

# build: push images, roll out jobs and services, deploy functions from source.
# Service and function deploys act as the app's runtime account, which the console
# may have created (jfrog-<name>), so actAs is project-wide in this test project.
resource "google_artifact_registry_repository_iam_member" "build_push" {
  project    = var.gcp_project_id
  location   = var.gcp_region
  repository = var.gcp_image_repository
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
