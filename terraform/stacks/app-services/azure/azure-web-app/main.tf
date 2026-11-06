# Azure: an App Service web app (Linux or Windows; Python, Java, .NET, Node.js or PHP code) reads JFrog
# artifacts with a short-lived OIDC token. Its own resource group, identity and plan: nothing is
# shared with another workload. No registry: the build pipeline resolves packages through JFrog
# with its OIDC token and zip-deploys code; at runtime the app exchanges its managed identity.
#
# One apply = one web app. Another OS/runtime is another deployment with its own workload name.
# Its JFROG_* settings and JFrog mapping belong to the console's workspace (azure-workload-onboard).

locals {
  linux   = var.os == "linux"
  version = coalesce(var.runtime_version, { python = "3.13", java = "21", dotnet = "10.0", node = local.linux ? "24-lts" : "~24", php = "8.4" }[var.runtime])
  # The platform's start command; Java and .NET start their own entry point. gunicorn comes
  # from the bundled packages (PYTHONPATH), so it runs as a module.
  # App Service evals this in a shell: no quotes or parentheses (the app comes from wsgi_app in gunicorn.conf.py).
  # Node runs server.js; PHP swaps in its nginx site (every path to index.php) and reloads nginx.
  startup = {
    python = "python -m gunicorn --config gunicorn.conf.py", java = "", dotnet = "",
    node   = "node server.js", php = "bash /home/site/wwwroot/startup.sh"
  }[var.runtime]
  linux_only = ["python", "php"]
  # Slots need Standard or higher (not Free, Shared or Basic).
  slots_allowed = !contains(["F1", "D1", "B1", "B2", "B3"], upper(var.sku))
  # The build pipeline reads these to pick its build (pip, Maven or dotnet).
  tags = merge(var.tags, { "jfrog-oidc-os" = var.os, "jfrog-oidc-runtime" = var.runtime, "jfrog-oidc-version" = local.version })
  settings = {
    # A system-assigned identity needs no client ID (null with SystemAssigned, left out).
    AZURE_CLIENT_ID = module.base.identity.client_id
    # The pipeline ships a built package: dependencies already came through JFrog.
    SCM_DO_BUILD_DURING_DEPLOYMENT = "false"
    WEBSITES_PORT                  = var.runtime == "python" ? "8000" : null
    # Vendored Python packages, installed by the pipeline from JFrog.
    PYTHONPATH = var.runtime == "python" ? "/home/site/wwwroot/.python_packages" : null
  }
}

module "base" {
  source                = "../../../../modules/azure-workload"
  workload              = var.workload
  azure_subscription_id = var.azure_subscription_id
  location              = var.azure_location
  identity_type         = var.identity_type
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
      condition     = !(var.os == "windows" && contains(local.linux_only, var.runtime))
      error_message = "Python and PHP run on Linux App Service only: use os = linux, or java/dotnet/node on Windows."
    }
    precondition {
      condition     = var.slot == "" || local.slots_allowed
      error_message = "Deployment slots need a Standard (S1) or higher plan: set sku = S1 or leave slot empty."
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
  tags                                           = local.tags

  identity {
    type         = var.identity_type
    identity_ids = var.identity_type == "UserAssigned" ? [module.base.identity.id] : null
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
      node_version        = var.runtime == "node" ? local.version : null
      php_version         = var.runtime == "php" ? local.version : null
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
  tags                                           = local.tags

  identity {
    type         = var.identity_type
    identity_ids = var.identity_type == "UserAssigned" ? [module.base.identity.id] : null
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
      node_version                 = var.runtime == "node" ? local.version : null
    }
  }

  lifecycle {
    ignore_changes = [app_settings]
  }
}

# Optional deployment slot: same identity and settings, so the exchange works in it before and
# after a swap. The console's wire step sets JFROG_* on production and on every slot.
resource "azurerm_linux_web_app_slot" "slot" {
  count                                          = local.linux && var.slot != "" ? 1 : 0
  name                                           = var.slot
  app_service_id                                 = azurerm_linux_web_app.workload[0].id
  https_only                                     = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  key_vault_reference_identity_id                = module.base.identity.id
  app_settings                                   = { for k, v in local.settings : k => v if v != null }
  tags                                           = local.tags

  identity {
    type         = var.identity_type
    identity_ids = var.identity_type == "UserAssigned" ? [module.base.identity.id] : null
  }

  site_config {
    always_on                         = true
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
      node_version        = var.runtime == "node" ? local.version : null
      php_version         = var.runtime == "php" ? local.version : null
    }
  }

  lifecycle {
    ignore_changes = [app_settings]
  }
}

resource "azurerm_windows_web_app_slot" "slot" {
  count                                          = !local.linux && var.slot != "" ? 1 : 0
  name                                           = var.slot
  app_service_id                                 = azurerm_windows_web_app.workload[0].id
  https_only                                     = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  key_vault_reference_identity_id                = module.base.identity.id
  app_settings                                   = { for k, v in local.settings : k => v if v != null }
  tags                                           = local.tags

  identity {
    type         = var.identity_type
    identity_ids = var.identity_type == "UserAssigned" ? [module.base.identity.id] : null
  }

  site_config {
    always_on                         = true
    ftps_state                        = "Disabled"
    health_check_path                 = "/health"
    health_check_eviction_time_in_min = 10

    application_stack {
      current_stack                = var.runtime
      dotnet_version               = var.runtime == "dotnet" ? "v${local.version}" : null
      java_version                 = var.runtime == "java" ? local.version : null
      java_embedded_server_enabled = var.runtime == "java" ? true : null
      node_version                 = var.runtime == "node" ? local.version : null
    }
  }

  lifecycle {
    ignore_changes = [app_settings]
  }
}

locals {
  app = local.linux ? azurerm_linux_web_app.workload[0] : azurerm_windows_web_app.workload[0]
}

# Each slot has its own system-assigned identity; production and every slot need the JFrog app role
# and mapping (azure-web-app-jfrog), since JFROG_* move with a swap.
locals {
  runtime_principal_ids = var.identity_type == "UserAssigned" ? { production = module.base.identity.principal_id } : merge(
    { production = local.app.identity[0].principal_id },
    var.slot == "" ? {} : { (var.slot) = one(concat(azurerm_linux_web_app_slot.slot[*].identity[0].principal_id, azurerm_windows_web_app_slot.slot[*].identity[0].principal_id)) },
  )
}
