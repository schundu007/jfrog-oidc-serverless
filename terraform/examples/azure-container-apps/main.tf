# Azure only, end to end: a Container Apps job reads JFrog artifacts with a
# short-lived OIDC token. Images come from ACR and are pulled with the job's own
# managed identity; JFrog holds artifacts (files, packages), not images. No stored
# credentials anywhere.
#
#   apply 1 (deploy_demo = false): resource group, ACR, Container Apps environment,
#           the job's managed identity, the Entra audience app, JFrog trust for
#           Entra tokens and the job's mapping.
#   then:   build samples/cloud-run-job into ACR (output image_build_command).
#   apply 2 (deploy_demo = true): the Container Apps job that exchanges its Entra
#           token for a 1 h JFrog token and downloads var.demo_artifact.

locals {
  name = "jfrog-oidc-demo"
  # ACR names are global, 5-50 alphanumerics: derive one from the subscription.
  acr_name = "jfrogoidc${substr(replace(var.azure_subscription_id, "-", ""), 0, 8)}"
}

resource "azurerm_resource_group" "demo" {
  name     = "${local.name}-rg"
  location = var.azure_location
}

resource "azurerm_log_analytics_workspace" "demo" {
  name                = "${local.name}-logs"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "demo" {
  name                       = "${local.name}-env"
  location                   = azurerm_resource_group.demo.location
  resource_group_name        = azurerm_resource_group.demo.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.demo.id
}

# Images live in ACR; the job pulls them with its own identity (AcrPull).
resource "azurerm_container_registry" "platform" {
  name                = local.acr_name
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azurerm_user_assigned_identity" "demo" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
}

resource "azurerm_role_assignment" "demo_acr_pull" {
  scope                = azurerm_container_registry.platform.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.demo.principal_id
  principal_type       = "ServicePrincipal"
}

# Entra audience app (the runtime exchange needs it); no pull path.
module "azure" {
  source = "../../modules/azure-identity-and-pull-path"

  name_prefix         = "jfrogdemo"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  jfrog_url           = var.jfrog_url
  jfrog_provider_name = module.jfrog_oidc.azure_provider_name
  pull_path_enabled   = false

  audience_assignees = {
    (local.name) = azurerm_user_assigned_identity.demo.principal_id
  }
}

module "jfrog_oidc" {
  source = "../../modules/jfrog-oidc"

  azure = {
    provider_name   = "azure-entra"
    tenant_id       = module.azure.tenant_id
    audience_app_id = module.azure.audience_app_id
  }

  workloads = {
    "azure-aca-demo" = {
      cloud       = "azure"
      subject     = azurerm_user_assigned_identity.demo.principal_id
      groups      = [var.jfrog_reader_group]
      expires_in  = 3600
      description = "Container Apps demo job runtime token"
    }
  }
}

# Runtime artifact read: the job exchanges its Entra token for a 1 h JFrog token
# (client/ library) and downloads var.demo_artifact.
resource "azurerm_container_app_job" "runtime_demo" {
  count                        = var.deploy_demo ? 1 : 0
  name                         = "jfrog-runtime-demo"
  location                     = azurerm_resource_group.demo.location
  resource_group_name          = azurerm_resource_group.demo.name
  container_app_environment_id = azurerm_container_app_environment.demo.id
  replica_timeout_in_seconds   = 300
  replica_retry_limit          = 0

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.demo.id]
  }

  registry {
    server   = azurerm_container_registry.platform.login_server
    identity = azurerm_user_assigned_identity.demo.id
  }

  template {
    container {
      name   = "demo"
      image  = "${azurerm_container_registry.platform.login_server}/${var.demo_image}"
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
        value = azurerm_user_assigned_identity.demo.client_id
      }
      env {
        name  = "DEMO_ARTIFACT"
        value = var.demo_artifact
      }
    }
  }

  depends_on = [azurerm_role_assignment.demo_acr_pull, module.azure]
}
