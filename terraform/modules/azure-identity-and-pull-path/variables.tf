variable "name_prefix" {
  type    = string
  default = "jfrog"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,17}$", var.name_prefix))
    error_message = "name_prefix: 2-18 chars, lowercase letters, digits, hyphens (Container Apps job names are capped at 32)."
  }
}

variable "location" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "jfrog_url" {
  description = "JFrog platform URL, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    # Origin only: the client and rotator append /access/... and /artifactory/...
    # themselves (docs/aks-pilot-lessons.md: the doubled-URL failure).
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only, e.g. https://acme.jfrog.io: https, no path, no trailing slash."
  }
}

variable "jfrog_provider_name" {
  description = "JFrog OIDC integration name for Entra tokens (jfrog-oidc module output azure_provider_name)."
  type        = string
}

variable "registry_username" {
  description = "token_spec.username of the image-pull mapping. Container Apps and Functions use it as the registry username."
  type        = string
  default     = null
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
  description = "Runtime managed identities allowed to request audience tokens: name => principal (object) ID. The rotator is added automatically."
  type        = map(string)
  default     = {}
}

variable "pull_path_enabled" {
  description = "Create the rotator, Key Vault secret and job. False when no app in this subscription pulls images through JFrog (e.g. only Flex Consumption apps); the audience app is still created for the runtime exchange."
  type        = bool
  default     = true
}

variable "key_vault_id" {
  description = "Existing Key Vault (RBAC authorization mode) that holds the registry token. Required when pull_path_enabled."
  type        = string
  default     = null
}

variable "key_vault_uri" {
  description = "Data-plane URI of that vault, e.g. https://kv-platform.vault.azure.net/"
  type        = string
  default     = null
}

variable "secret_readers" {
  description = "Identities whose apps read the registry token (Container Apps, Functions): name => principal ID. Granted Key Vault Secrets User on that one secret."
  type        = map(string)
  default     = {}
}

variable "function_app_ids" {
  description = <<-EOT
    Premium/Dedicated function apps (Microsoft.Web/sites) that pull a custom
    container from JFrog: name => ARM ID. The rotator forces their Key Vault
    references to refresh after each rotation instead of waiting up to 24h.
  EOT
  type        = map(string)
  default     = {}
}

variable "function_refresh_actions" {
  description = "Actions for the rotator's custom refresh role. Verify the name with `az provider operation show --namespace Microsoft.Web`."
  type        = list(string)
  default     = ["Microsoft.Web/sites/config/configreferences/appsettings/refresh/action"]
}

variable "container_app_environment_id" {
  description = "Container Apps environment that runs the rotator job."
  type        = string
  default     = null
}

variable "rotator_image" {
  description = "Rotator image. Host it in ACR, not behind JFrog: if the token lapses the rotator must still start."
  type        = string
  default     = null
}

variable "rotator_registry" {
  description = "ACR hosting the rotator image; the rotator identity gets AcrPull on it. Null for a public image."
  type = object({
    server = string
    acr_id = string
  })
  default = null
}

variable "rotation_cron" {
  description = "Cron (UTC) for the rotator job."
  type        = string
  default     = "23 * * * *"
}

variable "min_ttl_seconds" {
  description = "Rotator fails if JFrog issues a token with less lifetime than this. Matches rotate.py's default; size the mapping TTL from the README's TTL table, not from this floor."
  type        = number
  default     = 1800
}

variable "alert" {
  description = <<-EOT
    Optional log alert when no rotation succeeded within `window` (ISO 8601).
    Validate the query against your environment's log table first: environments
    sending logs via Log Analytics use ContainerAppConsoleLogs_CL; those using
    Azure Monitor diagnostic settings use ContainerAppConsoleLogs.
  EOT
  type = object({
    log_analytics_workspace_id = string
    action_group_id            = string
    window                     = optional(string, "PT3H")
    query                      = optional(string)
  })
  default = null

  validation {
    condition = var.alert == null || contains(
      ["PT1M", "PT5M", "PT10M", "PT15M", "PT30M", "PT45M", "PT1H", "PT2H", "PT3H", "PT4H", "PT5H", "PT6H", "P1D", "P2D"],
      try(var.alert.window, "PT3H")
    )
    error_message = "alert.window must be one of PT1M…PT6H, P1D or P2D (azurerm scheduled query rule limits), and shorter than the token TTL."
  }
}
