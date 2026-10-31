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

variable "ado_tenant_id" {
  description = "Entra tenant that issues the ADO service connections' tokens (issuer https://login.microsoftonline.com/<tenant>/v2.0)."
  type        = string
}

variable "ado_token_audience" {
  description = "aud of the token a job gets from System.OidcRequestUri (check with: python -m jfrog_oidc claims)."
  type        = string
  default     = "api://AzureADTokenExchange"
}

variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "gcp_image_repository" {
  description = "Artifact Registry repository the build pipeline pushes to."
  type        = string
  default     = "platform"
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

variable "jfrog_reader_group" {
  description = "Group the build mapping gets (read, plus cache on remote repos)."
  type        = string
}
