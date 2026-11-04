# Azure: an App Service web app (Linux or Windows; Python, Java or .NET code) reads JFrog
# artifacts with a short-lived OIDC token. Its own resource group, identity and plan: nothing is
# shared with another workload. No registry: the build pipeline resolves packages through JFrog
# with its OIDC token and zip-deploys code; at runtime the app exchanges its managed identity.
#
# One apply = one web app. Another OS/runtime is another deployment with its own workload name.
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (azure-workload-onboard).

locals {
  linux   = var.os == "linux"
  version = coalesce(var.runtime_version, { python = "3.12", java = "21", dotnet = "8.0" }[var.runtime])
  # The platform's start command; Java and .NET start their own entry point.
  startup = { python = "gunicorn --bind=0.0.0.0:8000 --timeout 60 app:app", java = "", dotnet = "" }[var.runtime]
  settings = {
    AZURE_CLIENT_ID = module.base.identity.client_id
    # The pipeline ships a built package: dependencies already came through JFrog.
    SCM_DO_BUILD_DURING_DEPLOYMENT = "false"
    WEBSITES_PORT                  = "8000"
    # Vendored Python packages, installed by the pipeline from JFrog.
    PYTHONPATH = var.runtime == "python" ? "/home/site/wwwroot/.python_packages" : null
  }
}

module "base" {
  source                = "../../../../modules/azure-workload"
  workload              = var.workload
  azure_subscription_id = var.azure_subscription_id
  location              = var.azure_location
  deployer_principal_id = var.deployer_principal_id
  tags                  = var.tags
}

resource "azurerm_service_plan" "this" {
  name                = "asp-${var.workload}"
  location            = module.base.location
  resource_group_name = module.base.resource_group_name
  os_type             = local.linux ? "Linux" : "Windows"
  sku_name            = var.sku
  tags                = var.tags

  lifecycle {
    precondition {
      condition     = !(var.os == "windows" && var.runtime == "python")
      error_message = "Python runs on Linux App Service only: use os = linux, or java/dotnet on Windows."
    }
  }
}

resource "azurerm_linux_web_app" "workload" {
  count                                          = local.linux ? 1 : 0
  name                                           = var.workload
  location                                       = module.base.location
  resource_group_name                            = module.base.resource_group_name
  service_plan_id                                = azurerm_service_plan.this.id
  https_only                                     = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  key_vault_reference_identity_id                = module.base.identity.id
  app_settings                                   = { for k, v in local.settings : k => v if v != null }
  tags                                           = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [module.base.identity.id]
  }

  site_config {
    always_on                         = var.sku != "F1"
    ftps_state                        = "Disabled"
    health_check_path                 = "/health"
    health_check_eviction_time_in_min = 10
    app_command_line                  = local.startup

    application_stack {
      python_version      = var.runtime == "python" ? local.version : null
      java_version        = var.runtime == "java" ? local.version : null
      java_server         = var.runtime == "java" ? "JAVA" : null
      java_server_version = var.runtime == "java" ? local.version : null
      dotnet_version      = var.runtime == "dotnet" ? local.version : null
    }
  }

  lifecycle {
    # The console's wire step owns JFROG_*; the pipeline owns the code.
    ignore_changes = [app_settings]
  }
}

resource "azurerm_windows_web_app" "workload" {
  count                                          = local.linux ? 0 : 1
  name                                           = var.workload
  location                                       = module.base.location
  resource_group_name                            = module.base.resource_group_name
  service_plan_id                                = azurerm_service_plan.this.id
  https_only                                     = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  key_vault_reference_identity_id                = module.base.identity.id
  app_settings                                   = { for k, v in local.settings : k => v if v != null }
  tags                                           = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [module.base.identity.id]
  }

  site_config {
    always_on                         = var.sku != "F1"
    ftps_state                        = "Disabled"
    health_check_path                 = "/health"
    health_check_eviction_time_in_min = 10

    application_stack {
      current_stack                = var.runtime
      dotnet_version               = var.runtime == "dotnet" ? "v${local.version}" : null
      java_version                 = var.runtime == "java" ? local.version : null
      java_embedded_server_enabled = var.runtime == "java" ? true : null
    }
  }

  lifecycle {
    ignore_changes = [app_settings]
  }
}

locals {
  app = local.linux ? azurerm_linux_web_app.workload[0] : azurerm_windows_web_app.workload[0]
}
