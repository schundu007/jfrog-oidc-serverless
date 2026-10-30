# Azure side.
#
# 1. An Entra app registration acts as the token *audience*. Managed identities
#    request a token for api://<app-id>; with requestedAccessTokenVersion = 2 the
#    token has iss=https://login.microsoftonline.com/<tenant>/v2.0, aud=<app-id>,
#    oid=sub=<identity principal id>. JFrog trusts that issuer and maps on oid.
# 2. Pull path: a scheduled Container Apps job (own managed identity) exchanges
#    its token for a short-lived read-only JFrog token and writes it to one Key
#    Vault secret. Container Apps registries and Functions custom-container
#    settings reference that secret versionlessly.

data "azurerm_client_config" "current" {}

locals {
  # The audience app is always needed (runtime exchange). Everything from the
  # rotator identity down exists only when some app pulls images through JFrog.
  pull = var.pull_path_enabled ? 1 : 0
}

# --- audience app ---------------------------------------------------------------

resource "random_uuid" "exchange_role" {}

resource "azuread_application" "jfrog_audience" {
  display_name     = "${var.name_prefix}-jfrog-oidc-audience"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]

  api {
    requested_access_token_version = 2
  }

  app_role {
    allowed_member_types = ["Application"]
    description          = "Obtain tokens to exchange for short-lived JFrog access tokens"
    display_name         = "JFrog.Exchange"
    enabled              = true
    id                   = random_uuid.exchange_role.result
    value                = "JFrog.Exchange"
  }

  lifecycle {
    ignore_changes = [identifier_uris]
    precondition {
      condition = !var.pull_path_enabled || alltrue([
        for v in [var.key_vault_id, var.key_vault_uri, var.container_app_environment_id, var.rotator_image, var.registry_username] : v != null && v != ""
      ])
      error_message = "pull_path_enabled needs key_vault_id, key_vault_uri, container_app_environment_id, rotator_image and registry_username."
    }
  }
}

resource "azuread_application_identifier_uri" "jfrog_audience" {
  application_id = azuread_application.jfrog_audience.id
  identifier_uri = "api://${azuread_application.jfrog_audience.client_id}"
}

resource "azuread_service_principal" "jfrog_audience" {
  client_id                    = azuread_application.jfrog_audience.client_id
  app_role_assignment_required = var.require_app_role_assignment
  owners                       = [data.azurerm_client_config.current.object_id]
}

# --- rotator identity -------------------------------------------------------------

resource "azurerm_user_assigned_identity" "rotator" {
  count = local.pull

  name                = "${var.name_prefix}-rotator"
  location            = var.location
  resource_group_name = var.resource_group_name
}

locals {
  assignees = merge(var.audience_assignees, var.pull_path_enabled ? {
    "${var.name_prefix}-rotator" = azurerm_user_assigned_identity.rotator[0].principal_id
  } : {})
  secret_scope = var.pull_path_enabled ? "${var.key_vault_id}/secrets/${azurerm_key_vault_secret.registry_token[0].name}" : null
  rotator      = var.pull_path_enabled ? azurerm_user_assigned_identity.rotator[0] : null
  audience_uri = "api://${azuread_application.jfrog_audience.client_id}"
}

resource "azuread_app_role_assignment" "exchange" {
  for_each = var.require_app_role_assignment ? local.assignees : {}

  app_role_id         = random_uuid.exchange_role.result
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.jfrog_audience.object_id
}

# --- registry token secret ----------------------------------------------------------

resource "azurerm_key_vault_secret" "registry_token" {
  count = local.pull

  name         = "${var.name_prefix}-registry-token"
  key_vault_id = var.key_vault_id
  # Write-only: a normal `value` is re-read on every refresh and would put the
  # live JFrog token into Terraform state.
  value_wo         = "bootstrap-run-the-rotator"
  value_wo_version = 1
  content_type     = "jfrog-access-token"
  tags = {
    username   = var.registry_username
    rotated-by = "jfrog-oidc-rotator"
  }

  # The rotator owns value, tags and expiry after the first run.
  lifecycle {
    ignore_changes = [tags, content_type, expiration_date, not_before_date]
  }
}

resource "azurerm_role_assignment" "rotator_writes_secret" {
  count = local.pull

  scope                = local.secret_scope
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = local.rotator.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "apps_read_secret" {
  for_each = var.pull_path_enabled ? var.secret_readers : {}

  scope                = local.secret_scope
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value
  principal_type       = "ServicePrincipal"
}

# Refreshing Key Vault references needs one action. Website Contributor would
# let the rotator deploy code into the app and run as its identity, so use a
# custom role. Confirm the action name in your tenant with:
#   az provider operation show --namespace Microsoft.Web \
#     --query "resourceTypes[?name=='sites/config'].operations[].name" -o tsv | grep -i refresh
resource "azurerm_role_definition" "kv_reference_refresh" {
  count = var.pull_path_enabled && length(var.function_app_ids) > 0 ? 1 : 0

  name        = "${var.name_prefix}-kv-reference-refresh"
  scope       = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"
  description = "Refresh Key Vault references on App Service / Functions apps. Nothing else."

  permissions {
    actions = var.function_refresh_actions
  }

  assignable_scopes = ["/subscriptions/${data.azurerm_client_config.current.subscription_id}"]
}

resource "azurerm_role_assignment" "rotator_refreshes_functions" {
  for_each = var.pull_path_enabled ? var.function_app_ids : {}

  scope              = each.value
  role_definition_id = azurerm_role_definition.kv_reference_refresh[0].role_definition_resource_id
  principal_id       = local.rotator.principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_assignment" "rotator_pulls_own_image" {
  count = var.pull_path_enabled && var.rotator_registry != null ? 1 : 0

  scope                = var.rotator_registry.acr_id
  role_definition_name = "AcrPull"
  principal_id         = local.rotator.principal_id
  principal_type       = "ServicePrincipal"
}

# --- rotator job ----------------------------------------------------------------------

resource "azurerm_container_app_job" "rotator" {
  count = local.pull

  name                         = "${var.name_prefix}-rotator"
  location                     = var.location
  resource_group_name          = var.resource_group_name
  container_app_environment_id = var.container_app_environment_id
  replica_timeout_in_seconds   = 300
  replica_retry_limit          = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [local.rotator.id]
  }

  schedule_trigger_config {
    cron_expression          = var.rotation_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  dynamic "registry" {
    for_each = var.rotator_registry == null ? [] : [var.rotator_registry]
    content {
      server   = registry.value.server
      identity = local.rotator.id
    }
  }

  template {
    container {
      name   = "rotator"
      image  = var.rotator_image
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "ROTATOR_TARGET"
        value = "azure"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = local.rotator.client_id
      }
      env {
        name  = "JFROG_URL"
        value = var.jfrog_url
      }
      env {
        name  = "JFROG_OIDC_PROVIDER"
        value = var.jfrog_provider_name
      }
      env {
        name  = "JFROG_OIDC_AUDIENCE"
        value = local.audience_uri
      }
      env {
        name  = "KEY_VAULT_URI"
        value = var.key_vault_uri
      }
      env {
        name  = "KEY_VAULT_SECRET"
        value = azurerm_key_vault_secret.registry_token[0].name
      }
      env {
        name  = "FUNCTION_APP_IDS"
        value = join(",", values(var.function_app_ids))
      }
      env {
        name  = "MIN_TTL_SECONDS"
        value = tostring(var.min_ttl_seconds)
      }
    }
  }

  depends_on = [
    azuread_app_role_assignment.exchange,
    azurerm_role_assignment.rotator_writes_secret,
    azurerm_role_assignment.rotator_pulls_own_image,
  ]
}

# --- optional alert ------------------------------------------------------------------

locals {
  default_alert_query = <<-KQL
    ContainerAppConsoleLogs_CL
    | where ContainerJobName_s == "${var.name_prefix}-rotator"
    | where Log_s has "rotation_succeeded"
    | summarize successes = count()
  KQL
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "rotation_stale" {
  count = var.pull_path_enabled && var.alert != null ? 1 : 0

  name                 = "${var.name_prefix}-jfrog-token-not-rotated"
  location             = var.location
  resource_group_name  = var.resource_group_name
  scopes               = [var.alert.log_analytics_workspace_id]
  description          = "No successful JFrog registry token rotation; Container Apps and Functions will fail new pulls when the current token expires."
  severity             = 2
  evaluation_frequency = "PT15M"
  window_duration      = var.alert.window

  criteria {
    query                   = coalesce(var.alert.query, local.default_alert_query)
    time_aggregation_method = "Total"
    metric_measure_column   = "successes"
    operator                = "LessThan"
    threshold               = 1
    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [var.alert.action_group_id]
  }
}

# Resources that gained count with pull_path_enabled keep their state.
moved {
  from = azurerm_user_assigned_identity.rotator
  to   = azurerm_user_assigned_identity.rotator[0]
}

moved {
  from = azurerm_key_vault_secret.registry_token
  to   = azurerm_key_vault_secret.registry_token[0]
}

moved {
  from = azurerm_role_assignment.rotator_writes_secret
  to   = azurerm_role_assignment.rotator_writes_secret[0]
}

moved {
  from = azurerm_container_app_job.rotator
  to   = azurerm_container_app_job.rotator[0]
}
