# JFrog OIDC trust for this cloud's ADO pipelines, made after the infrastructure exists.
# gcp-foundation creates only cloud and ADO resources (service connections, identities, state);
# everything about JFrog lives here: the integration ado-<org>-gcp, its build and setup
# mappings, and the pipelines' JFrog variable group jfrog-oidc-gcp-<env>-jfrog.
#   build -> reader group (read + remote cache), setup -> admin (workload onboarding)

locals {
  issuer = "https://login.microsoftonline.com/${var.ado_tenant_id}/v2.0"
}

data "azuredevops_project" "this" {
  name = var.ado_project
}

# The foundation's service connections, read by name: their subjects bind the mappings.
data "azuredevops_serviceendpoint_azurerm" "ado" {
  for_each = toset(["build", "setup"])

  project_id            = data.azuredevops_project.this.id
  service_endpoint_name = "jfrog-oidc-gcp-${each.key}"
}

resource "platform_oidc_configuration" "ado" {
  name          = "ado-${var.ado_org}-gcp"
  description   = "Azure DevOps ${var.ado_org} Google Cloud pipelines (Entra-issued service connection tokens)"
  issuer_url    = local.issuer
  provider_type = "generic"
  audience      = var.ado_token_audience
}

resource "platform_oidc_identity_mapping" "build" {
  name          = "ado-build"
  description   = "ADO build/deploy pipelines (jfrog-oidc-gcp-build)"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 1
  claims_json   = jsonencode({ sub = data.azuredevops_serviceendpoint_azurerm.ado["build"].workload_identity_federation_subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/groups:\"${var.jfrog_reader_group}\""
    audience   = "jfrt@*"
    expires_in = 3600
  }
}

resource "platform_oidc_identity_mapping" "setup" {
  name          = "ado-setup"
  description   = "ADO onboarding pipelines: JFrog admin for the OIDC workspaces (jfrog-oidc-gcp-setup)"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 2
  claims_json   = jsonencode({ sub = data.azuredevops_serviceendpoint_azurerm.ado["setup"].workload_identity_federation_subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/admin"
    username   = "ado-setup"
    audience   = "jfrt@* jfac@*"
    expires_in = 3600
  }
}

# What the build-deploy and onboarding pipelines read about JFrog (- group: jfrog-oidc-gcp-<env>-jfrog).
# The stack-setup (infrastructure) pipelines never reference it.
resource "azuredevops_variable_group" "jfrog" {
  project_id   = data.azuredevops_project.this.id
  name         = "jfrog-oidc-gcp-${var.pipeline_environment}-jfrog"
  description  = "Written by terraform/stacks/oidc/gcp/gcp-jfrog-trust; do not edit by hand."
  allow_access = true

  variable {
    name  = "JFROG_URL"
    value = var.jfrog_url
  }
  variable {
    name  = "JFROG_OIDC_PROVIDER"
    value = platform_oidc_configuration.ado.name
  }
}
