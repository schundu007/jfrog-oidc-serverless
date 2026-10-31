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
  description = "Name of the JFrog OIDC integration this stack creates for the operator (trusts Google ID tokens)."
  type        = string
  default     = "console-operator-google"
}

variable "console_jfrog_audience" {
  description = "Audience the operator's Google ID token carries."
  type        = string
  default     = "jfrog-console"
}

locals {
  console_operator = length(var.console_operators) > 0
}

resource "google_service_account" "console_operator" {
  depends_on = [google_project_service.apis]

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

# Its own integration, so it never depends on (or collides with) a workload's gcp-cloudrun one.
resource "platform_oidc_configuration" "console_operator" {
  count         = local.console_operator ? 1 : 0
  name          = var.console_jfrog_provider
  description   = "Console operator: Google ID tokens of the jfrog-console-operator service account"
  issuer_url    = "https://accounts.google.com"
  provider_type = "generic"
  audience      = var.console_jfrog_audience
}

resource "platform_oidc_identity_mapping" "console_operator" {
  count         = local.console_operator ? 1 : 0
  name          = "console-operator"
  description   = "Console operator: short-lived admin token for console reads and approved writes"
  provider_name = platform_oidc_configuration.console_operator[0].name
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
