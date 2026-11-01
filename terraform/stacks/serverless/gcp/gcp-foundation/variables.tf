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

