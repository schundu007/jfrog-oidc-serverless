variable "jfrog_url" {
  description = "JFrog platform origin, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only: https, no path, no trailing slash."
  }
}

variable "jfrog_reader_group" {
  description = "Group the build mapping gets (read, plus cache on remote repos)."
  type        = string
}

variable "ado_org" {
  description = "Azure DevOps organization name (used in resource names)."
  type        = string
}

variable "ado_project" {
  description = "Azure DevOps project that holds azure-foundation's service connections."
  type        = string
}

variable "azure_tenant_id" {
  description = "Entra tenant that issues the ADO service connections' tokens (issuer https://login.microsoftonline.com/<tenant>/v2.0)."
  type        = string
}

variable "ado_token_audience" {
  description = "aud of the token a job gets from System.OidcRequestUri (check with: python -m jfrog_oidc claims)."
  type        = string
  default     = "api://AzureADTokenExchange"
}

variable "pipeline_environment" {
  description = "Environment name the pipelines pass (parameters.environment); names the variable group."
  type        = string
  default     = "test"
}

variable "console_operators" {
  description = "Emails of the people who may act as the console operator (members or guests of the tenant). Empty: no operator identity."
  type        = list(string)
  default     = []
}
