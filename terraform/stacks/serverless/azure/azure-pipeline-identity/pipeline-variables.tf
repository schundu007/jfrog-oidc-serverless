data "azuredevops_project" "this" {
  name = var.ado_project
}

# Everything the azure-* pipelines read about their targets (- group: jfrog-oidc-azure-<environment>).
resource "azuredevops_variable_group" "azure" {
  project_id   = data.azuredevops_project.this.id
  name         = "jfrog-oidc-azure-${var.pipeline_environment}"
  description  = "Written by terraform/stacks/azure-pipeline-identity; do not edit by hand."
  allow_access = true

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
  # Where the Azure stacks' Terraform state lives: the JFrog trust pipeline (azure-jfrog-trust) keeps its state here.
  variable {
    name  = "TF_STATE_RESOURCE_GROUP"
    value = azurerm_resource_group.ado.name
  }
  variable {
    name  = "TF_STATE_ACCOUNT"
    value = azurerm_storage_account.tfstate.name
  }
}
