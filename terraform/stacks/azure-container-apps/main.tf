# Azure: a Container Apps job reads JFrog artifacts with a short-lived OIDC
# token. Images come from ACR and are pulled with the job's own managed identity;
# JFrog holds the artifacts. No stored credentials anywhere.
#
# Run by the ADO setup pipeline. The build pipeline pushes the image and rolls
# it out, so the job image is ignored here after creation.
#
#   apply 1 (deploy_job = false): resource group, ACR, Container Apps environment,
#           the workload's managed identity, the Entra audience app, JFrog trust
#           for Entra tokens and the workload's mapping.
#   then:   the build pipeline pushes apps/artifact-reader into ACR.
#   apply 2 (deploy_job = true): the Container Apps job.

locals {
  # ACR names are global, 5-50 alphanumerics: derive one unless given.
  acr_name = coalesce(var.acr_name, substr("cr${replace(var.workload, "-", "")}${replace(var.azure_subscription_id, "-", "")}", 0, 32))
}

resource "azurerm_resource_group" "workload" {
  name     = "rg-${var.workload}"
  location = var.azure_location
  tags     = var.tags
}

resource "azurerm_log_analytics_workspace" "workload" {
  name                = "log-${var.workload}"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_container_app_environment" "workload" {
  name                       = "cae-${var.workload}"
  location                   = azurerm_resource_group.workload.location
  resource_group_name        = azurerm_resource_group.workload.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.workload.id
  tags                       = var.tags
}

# Images live in ACR; the job pulls them with its own identity (AcrPull).
resource "azurerm_container_registry" "images" {
  name                = local.acr_name
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  sku                 = var.acr_sku
  admin_enabled       = false
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "workload" {
  name                = "id-${var.workload}"
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  tags                = var.tags
}

resource "azurerm_role_assignment" "workload_acr_pull" {
  scope                = azurerm_container_registry.images.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.workload.principal_id
  principal_type       = "ServicePrincipal"
}

# The deployer (ADO build pipeline identity) pushes images and rolls out the job.
resource "azurerm_role_assignment" "deployer_acr_push" {
  count                = var.deployer_principal_id == "" ? 0 : 1
  scope                = azurerm_container_registry.images.id
  role_definition_name = "AcrPush"
  principal_id         = var.deployer_principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deployer_contributor" {
  count                = var.deployer_principal_id == "" ? 0 : 1
  scope                = azurerm_resource_group.workload.id
  role_definition_name = "Contributor"
  principal_id         = var.deployer_principal_id
  principal_type       = "ServicePrincipal"
}

# Entra audience app (the runtime exchange needs it); no pull path.
module "azure" {
  source = "../../modules/azure-identity-and-pull-path"

  name_prefix         = var.workload
  location            = azurerm_resource_group.workload.location
  resource_group_name = azurerm_resource_group.workload.name
  jfrog_url           = var.jfrog_url
  jfrog_provider_name = module.jfrog_oidc.azure_provider_name
  pull_path_enabled   = false

  audience_assignees = {
    (var.workload) = azurerm_user_assigned_identity.workload.principal_id
  }
}

module "jfrog_oidc" {
  source = "../../modules/jfrog-oidc"

  azure = {
    provider_name   = var.jfrog_provider_name
    tenant_id       = module.azure.tenant_id
    audience_app_id = module.azure.audience_app_id
  }

  workloads = {
    (var.workload) = {
      cloud       = "azure"
      subject     = azurerm_user_assigned_identity.workload.principal_id
      groups      = [var.jfrog_reader_group]
      expires_in  = 3600
      description = "Container Apps job ${var.workload} (runtime read)"
    }
  }
}

resource "azurerm_container_app_job" "workload" {
  count                        = var.deploy_job ? 1 : 0
  name                         = "caj-${var.workload}"
  location                     = azurerm_resource_group.workload.location
  resource_group_name          = azurerm_resource_group.workload.name
  container_app_environment_id = azurerm_container_app_environment.workload.id
  replica_timeout_in_seconds   = 300
  replica_retry_limit          = 0
  tags                         = var.tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.workload.id]
  }

  registry {
    server   = azurerm_container_registry.images.login_server
    identity = azurerm_user_assigned_identity.workload.id
  }

  template {
    container {
      name   = var.workload
      image  = "${azurerm_container_registry.images.login_server}/${var.job_image}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "JFROG_URL"
        value = var.jfrog_url
      }
      env {
        name  = "JFROG_OIDC_PROVIDER"
        value = module.jfrog_oidc.azure_provider_name
      }
      env {
        name  = "JFROG_OIDC_AUDIENCE"
        value = module.azure.audience_uri
      }
      env {
        name  = "JFROG_OIDC_SOURCE"
        value = "azure"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.workload.client_id
      }
      env {
        name  = "JFROG_ARTIFACT_PATH"
        value = var.artifact_path
      }
    }
  }

  lifecycle {
    # The build pipeline owns the image tag.
    ignore_changes = [template[0].container[0].image]
  }

  depends_on = [azurerm_role_assignment.workload_acr_pull, module.azure]
}
