data "azuredevops_project" "this" {
  name = var.ado_project
}

# Everything the gcp-* pipelines read about their targets (- group: jfrog-oidc-gcp-<environment>).
resource "azuredevops_variable_group" "gcp" {
  project_id   = data.azuredevops_project.this.id
  name         = "jfrog-oidc-gcp-${var.pipeline_environment}"
  description  = "Written by terraform/stacks/gcp-foundation; do not edit by hand."
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
    name  = "BUILD_SERVICE_CONNECTION_ID"
    value = local.connections.build.id
  }
  variable {
    name  = "SETUP_SERVICE_CONNECTION_ID"
    value = local.connections.setup.id
  }
  variable {
    name  = "GCP_PROJECT"
    value = var.gcp_project_id
  }
  variable {
    name  = "GCP_REGION"
    value = var.gcp_region
  }
  variable {
    name  = "GCP_WIF_PROVIDER"
    value = "${local.pool}/providers/${google_iam_workload_identity_pool_provider.ado.workload_identity_pool_provider_id}"
  }
  variable {
    name  = "GCP_BUILD_SERVICE_ACCOUNT"
    value = google_service_account.ado["build"].email
  }
  variable {
    name  = "GCP_SETUP_SERVICE_ACCOUNT"
    value = google_service_account.ado["setup"].email
  }
  variable {
    name  = "IMAGE_REPOSITORY"
    value = google_artifact_registry_repository.images.repository_id
  }
}
