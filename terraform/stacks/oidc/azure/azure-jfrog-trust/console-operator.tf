# The console operator for Azure: no stored JFrog token on the operator's machine (as gcp-jfrog-trust
# does for GCP; the clouds share nothing). The console runs
#   az account get-access-token --scope api://jfrog-console-<org>/.default
# with the operator's own az login and exchanges that Entra token at JFrog for a short-lived admin
# token (console/jfrog_operator.py). Only var.console_operators get a token for the application
# (assignment required) and each has its own mapping, bound to their object id.
#
# The console reads console_operator_env from this root's state: nothing to set before starting it.
# The application exists only for the JFrog exchange, so it is OIDC configuration, not infrastructure.

locals {
  console_operator     = length(var.console_operators) > 0
  console_audience_uri = "api://jfrog-console-${var.ado_org}"
  azure_cli_client_id  = "04b07795-8ddb-461a-bbee-02f9e1bf7b46" # Azure CLI: gets the token without a consent prompt
  tenant_domains       = [for d in data.azuread_domains.tenant.domains : lower(d.domain_name)]
  initial_domain       = one([for d in data.azuread_domains.tenant.domains : d.domain_name if d.initial])
  # A guest's UPN is <email with @ as _>#EXT#@<initial domain>; a member's is the email.
  operator_upns = {
    for e in var.console_operators : e => (
      contains(local.tenant_domains, lower(split("@", e)[1])) ? e : "${replace(e, "@", "_")}#EXT#@${local.initial_domain}"
    )
  }
}

data "azuread_domains" "tenant" {}

data "azuread_user" "console_operator" {
  for_each            = local.operator_upns
  user_principal_name = each.value
}

resource "random_uuid" "console_scope" {}

resource "azuread_application" "console_operator" {
  count           = local.console_operator ? 1 : 0
  display_name    = "jfrog-console-operator-${var.ado_org}"
  identifier_uris = [local.console_audience_uri]
  owners          = [for u in data.azuread_user.console_operator : u.object_id]

  api {
    requested_access_token_version = 2 # issuer .../v2.0, aud = the client id: what the JFrog integration trusts

    oauth2_permission_scope {
      id                         = random_uuid.console_scope.result
      value                      = "jfrog.console"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "JFrog console operator"
      admin_consent_description  = "Exchange for a short-lived JFrog admin token in the console"
      user_consent_display_name  = "JFrog console operator"
      user_consent_description   = "Exchange for a short-lived JFrog admin token in the console"
    }
  }
}

resource "azuread_application_pre_authorized" "azure_cli" {
  count                = local.console_operator ? 1 : 0
  application_id       = azuread_application.console_operator[0].id
  authorized_client_id = local.azure_cli_client_id
  permission_ids       = [random_uuid.console_scope.result]
}

resource "azuread_service_principal" "console_operator" {
  count                        = local.console_operator ? 1 : 0
  client_id                    = azuread_application.console_operator[0].client_id
  app_role_assignment_required = true
}

resource "azuread_app_role_assignment" "console_operator" {
  for_each            = data.azuread_user.console_operator
  app_role_id         = "00000000-0000-0000-0000-000000000000" # default access
  principal_object_id = each.value.object_id
  resource_object_id  = azuread_service_principal.console_operator[0].object_id
}

# Its own integration, so it never depends on (or collides with) the workloads' or pipelines' ones.
resource "platform_oidc_configuration" "console_operator" {
  count         = local.console_operator ? 1 : 0
  name          = "console-operator-entra"
  description   = "Console operator: Entra tokens for jfrog-console-operator-${var.ado_org}"
  issuer_url    = local.issuer
  provider_type = "generic"
  audience      = azuread_application.console_operator[0].client_id
}

resource "platform_oidc_identity_mapping" "console_operator" {
  for_each      = data.azuread_user.console_operator
  name          = "console-operator-${substr(each.value.object_id, 0, 8)}"
  description   = "Console operator ${each.key}: short-lived admin token for console reads and approved writes"
  provider_name = platform_oidc_configuration.console_operator[0].name
  priority      = 1
  claims_json   = jsonencode({ oid = each.value.object_id, aud = azuread_application.console_operator[0].client_id })
  token_spec = {
    scope      = "applied-permissions/admin"
    username   = "console-operator"
    audience   = "jfrt@* jfac@*"
    expires_in = 900
  }
}

output "console_operator_env" {
  description = "Environment for the consoles (configuration, not secrets)."
  value = local.console_operator ? {
    JFROG_OIDC_PROVIDER = platform_oidc_configuration.console_operator[0].name
    JFROG_OIDC_AUDIENCE = local.console_audience_uri
    JFROG_OIDC_SOURCE   = "azure"
  } : null
}
