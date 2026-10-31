data "azuredevops_project" "this" {
  name = var.ado_project
}

# Everything the azure-* pipelines read about their targets (- group: jfrog-oidc-azure-<environment>).
resource "azuredevops_variable_group" "azure" {
  project_id   = data.azuredevops_project.this.id
  name         = "jfrog-oidc-azure-${var.pipeline_environment}"
  description  = "Written by terraform/stacks/azure-foundation; do not edit by hand."
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
    name  = "AZURE_SUBSCRIPTION_ID"
    value = var.azure_subscription_id
  }
}
