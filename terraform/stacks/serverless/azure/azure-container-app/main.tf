# Azure: a Container App (HTTP service) reads JFrog artifacts with a short-lived OIDC token.
# Its own resource group, identity, registry and Container Apps environment: nothing is shared
# with another workload.
#
#   apply 1 (deploy_app = false): the base (resource group, identity, registry, environment).
#   then:   the build pipeline pushes apps/cloud-run-service into the registry.
#   apply 2 (deploy_app = true, app_image): the container app.
#
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (azure-workload-onboard).

module "base" {
  source                     = "../../../../modules/azure-workload"
  workload                   = var.workload
  azure_subscription_id      = var.azure_subscription_id
  location                   = var.azure_location
  deployer_principal_id      = var.deployer_principal_id
  registry                   = true
  container_apps_environment = true
  tags                       = var.tags
}

resource "azurerm_container_app" "workload" {
  count                        = var.deploy_app ? 1 : 0
  name                         = var.workload
  resource_group_name          = module.base.resource_group_name
  container_app_environment_id = module.base.container_app_environment_id
  revision_mode                = "Single"
  tags                         = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [module.base.identity.id]
  }

  registry {
    server   = module.base.acr_login_server
    identity = module.base.identity.id
  }

  ingress {
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 2
    container {
      name   = var.workload
      image  = "${module.base.acr_login_server}/${var.app_image}"
      cpu    = 0.25
      memory = "0.5Gi"
      env {
        name  = "AZURE_CLIENT_ID"
        value = module.base.identity.client_id
      }
    }
  }

  lifecycle {
    # The build pipeline owns the image tag; the console's wire step owns JFROG_*.
    ignore_changes = [template[0].container[0].image, template[0].container[0].env]
    precondition {
      condition     = var.app_image != ""
      error_message = "Set app_image (pushed by the build pipeline) before turning deploy_app on."
    }
  }

  depends_on = [module.base]
}
