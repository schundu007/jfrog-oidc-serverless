# JFrog OIDC trust for this cloud's ADO pipelines, made after the infrastructure exists.
# gcp-pipeline-identity creates only cloud and ADO resources (service connections, identities, state);
# everything about JFrog lives here: the integration ado-<org>-gcp, its build and setup
# mappings, and the pipelines' JFrog variable group jfrog-oidc-gcp-<env>-jfrog.
#   build -> reader group (read + remote cache) and publisher group (push to this cloud's
#            Docker repo, where build-deploy publishes each workload image), setup -> admin

locals {
  issuer = "https://login.microsoftonline.com/${var.ado_tenant_id}/v2.0"
}

data "azuredevops_project" "this" {
  name = var.ado_project
}

# The pipeline identity's service connections, read by name: their subjects bind the mappings.
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
    scope      = "applied-permissions/groups:\"${var.jfrog_reader_group}\",\"${platform_group.publishers.name}\""
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

# This cloud's own Docker repository: build-deploy pushes every workload image here first (the
# registry of record), then copies it to the stack's Artifact Registry repo for Cloud Run to pull.
resource "artifactory_local_docker_v2_repository" "images" {
  key             = var.docker_local_repo
  description     = "Workload images built by the Google Cloud ADO build-deploy pipelines"
  tag_retention   = 1
  max_unique_tags = 50
}

resource "platform_group" "publishers" {
  name        = "ado-${var.ado_org}-gcp-publishers"
  description = "ADO Google Cloud build pipelines (ado-build mapping): push images to ${var.docker_local_repo}"
}

resource "platform_permission" "publish" {
  name = "ado-${var.ado_org}-gcp-publish"
  artifact = {
    actions = {
      groups = [{ name = platform_group.publishers.name, permissions = ["READ", "ANNOTATE", "WRITE"] }]
    }
    targets = [{ name = artifactory_local_docker_v2_repository.images.key, include_patterns = ["**"] }]
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
  variable {
    name  = "JFROG_DOCKER_LOCAL"
    value = artifactory_local_docker_v2_repository.images.key
  }
}
