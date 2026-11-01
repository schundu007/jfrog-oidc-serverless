# Azure side of the runtime exchange.
#
# An Entra app registration acts as the token *audience*. Managed identities
# request a token for api://<app-id>; with requestedAccessTokenVersion = 2 the
# token has iss=https://login.microsoftonline.com/<tenant>/v2.0, aud=<app-id>,
# oid=sub=<identity principal id>. JFrog trusts that issuer and maps on oid.
#
# There is no image pull path on Azure: Azure signs in to a non-ACR registry only
# with a username and password. Images reach the app's own ACR through the ADO
# build pipeline (OIDC to JFrog), and the app pulls from ACR with its identity.

data "azurerm_client_config" "current" {}

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

resource "azuread_app_role_assignment" "exchange" {
  for_each = var.require_app_role_assignment ? var.audience_assignees : {}

  app_role_id         = random_uuid.exchange_role.result
  principal_object_id = each.value
  resource_object_id  = azuread_service_principal.jfrog_audience.object_id
}
