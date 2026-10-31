# Azure: a Functions app on its own Premium (or Dedicated) plan reads JFrog artifacts with a
# short-lived OIDC token. Code deploy (Python), so the platform never pulls from JFrog. Its own
# resource group, identity, plan and host storage: nothing is shared with another workload.
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
  sku_name            = var.plan_sku
  tags                = var.tags
}

resource "azurerm_linux_function_app" "workload" {
  # Function app names are global (<name>.azurewebsites.net).
  name                          = "${var.workload}-${substr(sha1(var.azure_subscription_id), 0, 6)}"
  location                      = module.base.location
  resource_group_name           = module.base.resource_group_name
  service_plan_id               = azurerm_service_plan.workload.id
  storage_account_name          = module.base.storage_account_name
  storage_uses_managed_identity = true
  https_only                    = true
  tags                          = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [module.base.identity.id]
  }

  site_config {
    application_stack {
      python_version = "3.12"
    }
  }

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
