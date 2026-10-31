# The consoles' operator identity: no stored JFrog token on the operator's machine.
# The console runs `gcloud auth print-identity-token --impersonate-service-account=<sa>
# --audiences=<aud>` with the operator's own gcloud login and exchanges that Google ID token
# at JFrog for a short-lived admin token (kcp_console/jfrog.py OperatorToken). Only members
# of var.console_operators can mint it (Token Creator on the service account).
#
# The console reads console_operator_env from this stack's state: nothing to set before starting it.

variable "console_operators" {
  description = "Who may act as the console operator, as IAM members (user:<email>). Empty: no operator identity."
  type        = list(string)
  default     = []
}

variable "console_jfrog_provider" {
  description = "JFrog OIDC provider that trusts Google ID tokens (issuer https://accounts.google.com)."
  type        = string
  default     = "gcp-cloudrun"
}

variable "console_jfrog_audience" {
  description = "That provider's audience."
  type        = string
  default     = "jfrog-gcp"
}

variable "azure_console_operators" {
  description = "Entra object IDs of people who run the Infrastructure page: read/write the Azure Terraform state (the pipelines' setup identity already can)."
  type        = list(string)
  default     = []
}

resource "azurerm_role_assignment" "console_operator_state" {
  for_each             = toset(var.azure_console_operators)
  scope                = azurerm_storage_container.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
  principal_type       = "User"
}

locals {
  console_operator = length(var.console_operators) > 0
}

resource "google_service_account" "console_operator" {
  count        = local.console_operator ? 1 : 0
  account_id   = "jfrog-console-operator"
  display_name = "JFrog console operator (OIDC exchange, no stored token)"
}

resource "google_service_account_iam_member" "console_operator" {
  for_each           = toset(var.console_operators)
  service_account_id = google_service_account.console_operator[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}

resource "platform_oidc_identity_mapping" "console_operator" {
  count         = local.console_operator ? 1 : 0
  name          = "console-operator"
  description   = "Console operator: short-lived admin token for console reads and approved writes"
  provider_name = var.console_jfrog_provider
  priority      = 1
  claims_json   = jsonencode({ sub = google_service_account.console_operator[0].unique_id, aud = var.console_jfrog_audience })
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
    JFROG_OIDC_PROVIDER        = var.console_jfrog_provider
    JFROG_OIDC_AUDIENCE        = var.console_jfrog_audience
    JFROG_OIDC_SERVICE_ACCOUNT = google_service_account.console_operator[0].email
  } : null
}
