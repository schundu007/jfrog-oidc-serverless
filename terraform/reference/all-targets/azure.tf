locals {
  azure_registry_username = "svc-azure-registry-pull"
  registry_secret_name    = "jfrog-registry-token"
  registry_secret_url     = module.azure.registry_secret_versionless_url
  storage_account_id      = var.azure_functions_storage_account_id

  # Same environment for every Azure runtime workload; only AZURE_CLIENT_ID differs.
  azure_runtime_env = {
    JFROG_URL           = var.jfrog_url
    JFROG_OIDC_PROVIDER = module.jfrog_oidc.azure_provider_name
    JFROG_OIDC_AUDIENCE = module.azure.audience_uri
    JFROG_OIDC_SOURCE   = "azure"
  }
}

# --- runtime identities -------------------------------------------------------------

resource "azurerm_user_assigned_identity" "aca_orders" {
  name                = "id-aca-orders"
  location            = var.azure_location
  resource_group_name = var.azure_resource_group_name
}

resource "azurerm_user_assigned_identity" "func_orders" {
  name                = "id-func-orders"
  location            = var.azure_location
  resource_group_name = var.azure_resource_group_name
}

resource "azurerm_role_assignment" "func_storage" {
  for_each = toset(["Storage Blob Data Owner", "Storage Queue Data Contributor", "Storage Table Data Contributor"])

  scope                = local.storage_account_id
  role_definition_name = each.value
  principal_id         = azurerm_user_assigned_identity.func_orders.principal_id
  principal_type       = "ServicePrincipal"
}

# --- identity + pull path -------------------------------------------------------------

module "azure" {
  source = "../../modules/azure-identity-and-pull-path"

  location                     = var.azure_location
  resource_group_name          = var.azure_resource_group_name
  jfrog_url                    = var.jfrog_url
  jfrog_provider_name          = module.jfrog_oidc.azure_provider_name
  registry_username            = local.azure_registry_username
  key_vault_id                 = var.azure_key_vault_id
  key_vault_uri                = var.azure_key_vault_uri
  container_app_environment_id = var.azure_container_app_environment_id
  rotator_image                = var.azure_rotator_image
  rotator_registry             = var.azure_rotator_acr

  audience_assignees = {
    aca-orders  = azurerm_user_assigned_identity.aca_orders.principal_id
    func-orders = azurerm_user_assigned_identity.func_orders.principal_id
  }

  secret_readers = {
    aca-orders  = azurerm_user_assigned_identity.aca_orders.principal_id
    func-orders = azurerm_user_assigned_identity.func_orders.principal_id
  }

  # Premium/Dedicated apps whose Key Vault references the rotator refreshes.
  # Static keys keep for_each plannable; the ID is known after the app exists.
  function_app_ids = {
    func-orders-premium = azurerm_linux_function_app.orders_premium.id
  }
}

# --- 1. Container App: image from JFrog + runtime exchange ------------------------------

resource "azurerm_container_app" "orders" {
  name                         = "aca-orders"
  resource_group_name          = var.azure_resource_group_name
  container_app_environment_id = var.azure_container_app_environment_id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aca_orders.id]
  }

  # Versionless reference: Container Apps fetches new versions within 30 minutes.
  secret {
    name                = local.registry_secret_name
    key_vault_secret_id = local.registry_secret_url
    identity            = azurerm_user_assigned_identity.aca_orders.id
  }

  registry {
    server               = local.docker_host
    username             = local.azure_registry_username
    password_secret_name = local.registry_secret_name
  }

  template {
    min_replicas = 0
    max_replicas = 10

    container {
      name   = "orders"
      image  = "${local.docker_host}/orders:1.4.2"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.aca_orders.client_id
      }

      dynamic "env" {
        for_each = local.azure_runtime_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [module.azure]
}

# --- 2. Functions on Container Apps (kind=functionapp): same registry wiring ------------

resource "azapi_resource" "func_orders_aca" {
  type      = "Microsoft.App/containerApps@${var.functions_on_aca_api_version}"
  name      = "func-orders-aca"
  location  = var.azure_location
  parent_id = "/subscriptions/${var.azure_subscription_id}/resourceGroups/${var.azure_resource_group_name}"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.func_orders.id]
  }

  body = {
    kind = "functionapp"
    properties = {
      managedEnvironmentId = var.azure_container_app_environment_id
      configuration = {
        ingress = { external = true, targetPort = 80 }
        secrets = [{
          name        = local.registry_secret_name
          keyVaultUrl = local.registry_secret_url
          identity    = azurerm_user_assigned_identity.func_orders.id
        }]
        registries = [{
          server            = local.docker_host
          username          = local.azure_registry_username
          passwordSecretRef = local.registry_secret_name
        }]
      }
      template = {
        containers = [{
          name      = "func"
          image     = "${local.docker_host}/func-orders:1.0.0"
          resources = { cpu = 0.5, memory = "1Gi" }
          env = concat(
            [
              { name = "AZURE_CLIENT_ID", value = azurerm_user_assigned_identity.func_orders.client_id },
              { name = "AzureWebJobsStorage__accountName", value = var.azure_functions_storage_account_name },
              { name = "AzureWebJobsStorage__credential", value = "managedidentity" },
              { name = "AzureWebJobsStorage__clientId", value = azurerm_user_assigned_identity.func_orders.client_id },
            ],
            [for k, v in local.azure_runtime_env : { name = k, value = v }],
          )
        }]
        scale = { minReplicas = 0, maxReplicas = 10 }
      }
    }
  }

  depends_on = [module.azure, azurerm_role_assignment.func_storage]
}

# --- 3. Premium / Dedicated function app with a custom container from JFrog ------------
# DOCKER_REGISTRY_SERVER_PASSWORD is a Key Vault reference. App Service caches
# references for up to 24h; the rotator calls the refresh API after each write.

resource "azurerm_linux_function_app" "orders_premium" {
  name                            = "func-orders-premium"
  location                        = var.azure_location
  resource_group_name             = var.azure_resource_group_name
  service_plan_id                 = var.azure_premium_plan_id
  storage_account_name            = var.azure_functions_storage_account_name
  storage_uses_managed_identity   = true
  key_vault_reference_identity_id = azurerm_user_assigned_identity.func_orders.id

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.func_orders.id]
  }

  site_config {
    application_stack {
      docker {
        registry_url      = "https://${local.docker_host}"
        image_name        = "func-orders"
        image_tag         = "1.0.0"
        registry_username = local.azure_registry_username
        registry_password = "@Microsoft.KeyVault(SecretUri=${local.registry_secret_url})"
      }
    }
  }

  app_settings = merge(local.azure_runtime_env, {
    AZURE_CLIENT_ID                     = azurerm_user_assigned_identity.func_orders.client_id
    AzureWebJobsStorage__clientId       = azurerm_user_assigned_identity.func_orders.client_id
    WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
  })

  depends_on = [azurerm_role_assignment.func_storage]
}

# --- 4. Flex Consumption (code): runtime exchange only ----------------------------------
# No image pull. Build dependencies in CI (CI OIDC -> JFrog) and deploy the
# package with remote build off, so the platform never needs a JFrog credential.
# azurerm_function_app_flex_consumption is recent; check argument names against
# the azurerm version you pin.

resource "azurerm_function_app_flex_consumption" "orders_flex" {
  name                              = "func-orders-flex"
  location                          = var.azure_location
  resource_group_name               = var.azure_resource_group_name
  service_plan_id                   = azurerm_service_plan.flex.id
  storage_container_type            = "blobContainer"
  storage_container_endpoint        = var.azure_flex_deployment_container_endpoint
  storage_authentication_type       = "UserAssignedIdentity"
  storage_user_assigned_identity_id = azurerm_user_assigned_identity.func_orders.id
  runtime_name                      = "python"
  runtime_version                   = "3.12"
  maximum_instance_count            = 50
  instance_memory_in_mb             = 2048

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.func_orders.id]
  }

  site_config {}

  app_settings = merge(local.azure_runtime_env, {
    AZURE_CLIENT_ID                  = azurerm_user_assigned_identity.func_orders.client_id
    AzureWebJobsStorage__accountName = var.azure_functions_storage_account_name
    AzureWebJobsStorage__credential  = "managedidentity"
    AzureWebJobsStorage__clientId    = azurerm_user_assigned_identity.func_orders.client_id
  })

  depends_on = [azurerm_role_assignment.func_storage]
}

resource "azurerm_service_plan" "flex" {
  name                = "asp-orders-flex"
  location            = var.azure_location
  resource_group_name = var.azure_resource_group_name
  os_type             = "Linux"
  sku_name            = "FC1"
}
