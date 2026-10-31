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

variable "connections" {
  description = <<-EOT
    ADO service connections (workload identity federation): build and setup.
    subject is the connection's workloadIdentityFederationSubject
    (az devops service-endpoint show ... --query authorization.parameters).
  EOT
  type = map(object({
    name    = string
    subject = string
  }))
  validation {
    condition     = toset(keys(var.connections)) == toset(["build", "setup"])
    error_message = "connections needs exactly the keys build and setup."
  }
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
