variable "audience" {
  description = <<-EOT
    Token audience the workloads request. "app_registration": an Entra app
    registration made here (api://<app-id>). "token_exchange": Microsoft's
    api://AzureADTokenExchange, nothing created in Entra.
  EOT
  type        = string
  default     = "app_registration"
  validation {
    condition     = contains(["app_registration", "token_exchange"], var.audience)
    error_message = "audience: app_registration or token_exchange."
  }
}

variable "name_prefix" {
  type    = string
  default = "jfrog"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,17}$", var.name_prefix))
    error_message = "name_prefix: 2-18 chars, lowercase letters, digits, hyphens."
  }
}

variable "require_app_role_assignment" {
  description = <<-EOT
    When true, only managed identities explicitly assigned the JFrog.Exchange app
    role can obtain a token for the audience at all. JFrog's claim matching on oid
    still applies; this adds a tenant-side gate in front of it.
  EOT
  type        = bool
  default     = true
}

variable "audience_assignees" {
  description = "Runtime managed identities allowed to request audience tokens: name => principal (object) ID."
  type        = map(string)
  default     = {}
}
