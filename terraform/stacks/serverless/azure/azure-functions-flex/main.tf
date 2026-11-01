# Azure: a Flex Consumption function app reads JFrog artifacts with a short-lived OIDC token.
# Code deploy, so the platform never pulls from JFrog. Its own resource group, identity, plan and
# host storage (also the deployment container): nothing is shared with another workload.
#
#   apply: everything; the build pipeline then deploys apps/azure-function as a package.
#
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (azure-workload-onboard).

module "base" {
  source                = "../../../../modules/azure-workload"
  workload              = var.workload
  azure_subscription_id = var.azure_subscription_id
  location              = var.azure_location
  deployer_principal_id = var.deployer_principal_id
  storage               = true
  tags                  = var.tags
}

resource "azurerm_service_plan" "workload" {
  name                = "asp-${var.workload}"
  location            = module.base.location
  resource_group_name = module.base.resource_group_name
  os_type             = "Linux"
  sku_name            = "FC1"
  tags                = var.tags
}

resource "azurerm_function_app_flex_consumption" "workload" {
  # Function app names are global (<name>.azurewebsites.net).
  name                              = "${var.workload}-${substr(sha1(var.azure_subscription_id), 0, 6)}"
  location                          = module.base.location
  resource_group_name               = module.base.resource_group_name
  service_plan_id                   = azurerm_service_plan.workload.id
  storage_container_type            = "blobContainer"
  storage_container_endpoint        = module.base.deployments_container_endpoint
  storage_authentication_type       = "UserAssignedIdentity"
  storage_user_assigned_identity_id = module.base.identity.id
  runtime_name                      = "python"
  runtime_version                   = "3.12"
  maximum_instance_count            = 40
  instance_memory_in_mb             = 2048
  tags                              = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [module.base.identity.id]
  }

  site_config {}

  app_settings = {
    AZURE_CLIENT_ID                  = module.base.identity.client_id
    AzureWebJobsStorage__accountName = module.base.storage_account_name
    AzureWebJobsStorage__credential  = "managedidentity"
    AzureWebJobsStorage__clientId    = module.base.identity.client_id
  }

  lifecycle {
    # The console's wire step owns JFROG_*; the build pipeline owns the package.
    ignore_changes = [app_settings]
  }

  depends_on = [module.base]
}
