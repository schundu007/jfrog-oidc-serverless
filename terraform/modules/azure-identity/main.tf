# Azure side of the runtime exchange.
#
# An Entra app registration acts as the token *audience*. Managed identities
# request a token for api://<app-id>; with requestedAccessTokenVersion = 2 the
# token has iss=https://login.microsoftonline.com/<tenant>/v2.0, aud=<app-id>,
# oid=sub=<identity principal id>. JFrog trusts that issuer and maps on oid.
#
# audience = "token_exchange" uses Microsoft's api://AzureADTokenExchange instead
# (no app registration): the token is v2 with aud = the first-party app ID below,
# the same issuer and oid. Nothing is created in Entra.
#
# There is no image pull path on Azure: Azure signs in to a non-ACR registry only
# with a username and password. Images reach the app's own ACR through the ADO
# build pipeline (OIDC to JFrog), and the app pulls from ACR with its identity.

data "azurerm_client_config" "current" {}

locals {
  app_registration = var.audience == "app_registration"
  # Microsoft's first-party AzureADTokenExchange app: the aud of its v2 tokens.
  token_exchange_app_id = "fb60f99c-7a34-4190-8149-302f77469936"
}

resource "random_uuid" "exchange_role" {
  count = local.app_registration ? 1 : 0
}

resource "azuread_application" "jfrog_audience" {
  count = local.app_registration ? 1 : 0

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
    id                   = random_uuid.exchange_role[0].result
    value                = "JFrog.Exchange"
  }

  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application_identifier_uri" "jfrog_audience" {
  count = local.app_registration ? 1 : 0

  application_id = azuread_application.jfrog_audience[0].id
  identifier_uri = "api://${azuread_application.jfrog_audience[0].client_id}"
}

resource "azuread_service_principal" "jfrog_audience" {
  count = local.app_registration ? 1 : 0

  client_id                    = azuread_application.jfrog_audience[0].client_id
  app_role_assignment_required = var.require_app_role_assignment
  owners                       = [data.azurerm_client_config.current.object_id]
}

resource "azuread_app_role_assignment" "exchange" {
  for_each = local.app_registration && var.require_app_role_assignment ? var.audience_assignees : {}

  app_role_id         = random_uuid.exchange_role[0].result
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.jfrog_audience[0].object_id
}

# The app registration resources gained count: existing state keeps them.
moved {
  from = random_uuid.exchange_role
  to   = random_uuid.exchange_role[0]
}

moved {
  from = azuread_application.jfrog_audience
  to   = azuread_application.jfrog_audience[0]
}

moved {
  from = azuread_application_identifier_uri.jfrog_audience
  to   = azuread_application_identifier_uri.jfrog_audience[0]
}

moved {
  from = azuread_service_principal.jfrog_audience
  to   = azuread_service_principal.jfrog_audience[0]
}
