variable "jfrog_url" {
  description = "JFrog platform origin, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only: https, no path, no trailing slash."
  }
}

variable "ado_org" {
  description = "Azure DevOps organization name (used in resource names)."
  type        = string
}

variable "azure_tenant_id" {
  description = "Entra tenant that issues the ADO service connections' tokens."
  type        = string
}

variable "azure_subscription_id" {
  type = string
}

variable "azure_identity" {
  description = "Managed identity behind the ADO service connections."
  type = object({
    name           = string
    resource_group = string
  })
}


variable "azure_console_operators" {
  description = "Entra object IDs of people who run the Infrastructure page: read/write the Azure Terraform state (the pipelines' setup identity already can)."
  type        = list(string)
  default     = []
}

variable "jfrog_reader_group" {
  description = "Group the build mapping gets (read, plus cache on remote repos)."
  type        = string
}

variable "ado_token_audience" {
  description = "aud of the token a job gets from System.OidcRequestUri (check with: python -m jfrog_oidc claims)."
  type        = string
  default     = "api://AzureADTokenExchange"
}

variable "ado_project" {
  description = "Azure DevOps project whose pipelines read this stack's variable group."
  type        = string
}

variable "pipeline_environment" {
  description = "Environment name the pipelines pass (parameters.environment); names the variable group."
  type        = string
  default     = "test"
}
