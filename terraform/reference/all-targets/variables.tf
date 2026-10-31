variable "jfrog_url" {
  description = "JFrog platform origin, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    # Origin only: the client and rotator append /access/... and /artifactory/...
    # themselves (docs/aks-pilot-lessons.md: the doubled-URL failure).
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only, e.g. https://acme.jfrog.io: https, no path, no trailing slash."
  }
}

variable "jfrog_docker_repo" {
  description = <<-EOT
    Docker repo key that images are pulled from. Workloads pull from the repo's
    dedicated host, <server>-<repo-key>.<domain> (JFrog's subdomain method), not
    from <server>.<domain>/<repo-key>/...: the repository-path method failed for
    local repos on the AKS pilot's instance.
  EOT
  type        = string
  default     = "docker-virtual"
}

variable "jfrog_docker_host" {
  description = "Override for the Docker registry host (bare host name, no https://). Null derives it from jfrog_url and jfrog_docker_repo."
  type        = string
  default     = null
  validation {
    condition     = var.jfrog_docker_host == null || can(regex("^[a-z0-9.-]+$", coalesce(var.jfrog_docker_host, "x")))
    error_message = "jfrog_docker_host is a bare host name such as acme-docker-virtual.jfrog.io: no scheme, no path."
  }
}

variable "jfrog_reader_group" {
  description = "Existing JFrog group with read-only permission on the repos serverless workloads pull from."
  type        = string
}

# --- GCP ---------------------------------------------------------------------------

variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "gcp_rotator_image" {
  description = "e.g. us-central1-docker.pkg.dev/<p>/platform/jfrog-oidc-rotator:0.1.0 (standard AR repo)"
  type        = string
}

variable "gcp_function_source" {
  description = "GCS object with the sample function's zipped source."
  type = object({
    bucket = string
    object = string
  })
}

variable "gcp_notification_channels" {
  type    = list(string)
  default = []
}

# --- Azure -------------------------------------------------------------------------

variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type    = string
  default = "westus2"
}

variable "azure_resource_group_name" {
  type = string
}

variable "azure_container_app_environment_id" {
  type = string
}

variable "azure_key_vault_id" {
  type = string
}

variable "azure_key_vault_uri" {
  type = string
}

variable "azure_rotator_image" {
  description = "e.g. platformacr.azurecr.io/jfrog-oidc-rotator:0.1.0"
  type        = string
}

variable "azure_rotator_acr" {
  type = object({
    server = string
    acr_id = string
  })
}

variable "azure_premium_plan_id" {
  description = "Elastic Premium or Dedicated Linux plan for the custom-container function app."
  type        = string
}

variable "azure_functions_storage_account_name" {
  description = "Storage account the function apps use, accessed with managed identity."
  type        = string
}

variable "azure_functions_storage_account_id" {
  type = string
}

variable "azure_flex_deployment_container_endpoint" {
  description = "Blob container URL for Flex Consumption deployment packages, e.g. https://<sa>.blob.core.windows.net/deployments"
  type        = string
}

variable "functions_on_aca_api_version" {
  description = "Microsoft.App API version that exposes kind=functionapp in your tenant."
  type        = string
  default     = "2025-01-01"
}
