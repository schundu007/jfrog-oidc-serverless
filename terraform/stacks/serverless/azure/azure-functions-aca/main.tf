# Azure: Functions on Container Apps (a container app of kind functionapp) reads JFrog artifacts
# with a short-lived OIDC token. Its own resource group, identity, registry, Container Apps
# environment and host storage: nothing is shared with another workload.
#
#   apply 1 (deploy_function = false): the base.
#   then:   the build pipeline pushes apps/azure-function (container) into the registry.
#   apply 2 (deploy_function = true, function_image): the function app.
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
  storage                    = true
  tags                       = var.tags
}

resource "azapi_resource" "workload" {
  count     = var.deploy_function ? 1 : 0
  type      = "Microsoft.App/containerApps@${var.functions_on_aca_api_version}"
  name      = var.workload
  location  = module.base.location
  parent_id = module.base.resource_group_id
  tags      = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [module.base.identity.id]
  }

  body = {
    kind = "functionapp"
    properties = {
      managedEnvironmentId = module.base.container_app_environment_id
      configuration = {
        ingress    = { external = true, targetPort = 80 }
        registries = [{ server = module.base.acr_login_server, identity = module.base.identity.id }]
      }
      template = {
        containers = [{
          name      = "func"
          image     = "${module.base.acr_login_server}/${var.function_image}"
          resources = { cpu = 0.5, memory = "1Gi" }
          env = [
            { name = "AZURE_CLIENT_ID", value = module.base.identity.client_id },
            { name = "AzureWebJobsStorage__accountName", value = module.base.storage_account_name },
            { name = "AzureWebJobsStorage__credential", value = "managedidentity" },
            { name = "AzureWebJobsStorage__clientId", value = module.base.identity.client_id },
          ]
        }]
        scale = { minReplicas = 0, maxReplicas = 2 }
      }
    }
  }

  lifecycle {
    # The build pipeline owns the image; the console's wire step owns JFROG_*.
    ignore_changes = [body.properties.template.containers]
    precondition {
      condition     = var.function_image != ""
      error_message = "Set function_image (pushed by the build pipeline) before turning deploy_function on."
    }
  }

  depends_on = [module.base]
}
