# Azure: a Container Apps job reads JFrog artifacts with a short-lived OIDC token. Its own
# resource group, identity, registry and Container Apps environment (modules/azure-workload):
# nothing is shared with another workload.
#
#   apply 1 (deploy_job = false): the base.
#   then:   the build pipeline pushes apps/artifact-reader into the registry.
#   apply 2 (deploy_job = true, job_image): the Container Apps job.
#
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (azure-workload-onboard).

module "base" {
  source                     = "../../../../modules/azure-workload"
  workload                   = var.workload
  azure_subscription_id      = var.azure_subscription_id
  location                   = var.azure_location
  identity_type              = var.identity_type
  registry                   = true
  acr_name                   = coalesce(var.acr_name, substr("cr${replace(var.workload, "-", "")}${replace(var.azure_subscription_id, "-", "")}", 0, 32))
  acr_sku                    = var.acr_sku
  container_apps_environment = true
  log_retention_days         = var.log_retention_days
  tags                       = var.tags
}

resource "azurerm_container_app_job" "workload" {
  count                        = var.deploy_job ? 1 : 0
  name                         = "caj-${var.workload}"
  location                     = module.base.location
  resource_group_name          = module.base.resource_group_name
  container_app_environment_id = module.base.container_app_environment_id
  replica_timeout_in_seconds   = 300
  replica_retry_limit          = 0
  tags                         = var.tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = var.identity_type
    identity_ids = var.identity_type == "UserAssigned" ? [module.base.identity.id] : null
  }

  registry {
    server   = module.base.acr_login_server
    identity = var.identity_type == "UserAssigned" ? module.base.identity.id : "System"
  }

  template {
    container {
      name   = var.workload
      image  = "${module.base.acr_login_server}/${var.job_image}"
      cpu    = 0.25
      memory = "0.5Gi"

      # A system-assigned identity needs no client ID.
      dynamic "env" {
        for_each = var.identity_type == "UserAssigned" ? [module.base.identity.client_id] : []
        content {
          name  = "AZURE_CLIENT_ID"
          value = env.value
        }
      }
    }
  }

  lifecycle {
    # The build pipeline owns the image tag; the console's wire step owns JFROG_*.
    ignore_changes = [template[0].container[0].image, template[0].container[0].env]
  }

  depends_on = [module.base]
}

locals {
  runtime_principal_id = var.identity_type == "UserAssigned" ? module.base.identity.principal_id : one(azurerm_container_app_job.workload[*].identity[0].principal_id)
}

