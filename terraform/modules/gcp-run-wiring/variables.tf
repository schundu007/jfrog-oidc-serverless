variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "kind" {
  description = "job (Cloud Run job), service (Cloud Run service, also the service behind a Cloud Run function) or worker_pool (Cloud Run worker pool)."
  type        = string

  validation {
    condition     = contains(["job", "service", "worker_pool"], var.kind)
    error_message = "kind must be job, service or worker_pool."
  }
}

variable "name" {
  type = string
}

variable "env" {
  description = "The JFrog settings to set on the first container. Every other variable stays as it is."
  type        = map(string)
}

variable "managed_keys" {
  description = "Variables this module owns: removed when it no longer sets them, and on destroy."
  type        = list(string)
  default     = ["JFROG_URL", "JFROG_OIDC_PROVIDER", "JFROG_OIDC_AUDIENCE", "JFROG_OIDC_SOURCE"]
}

variable "service_account" {
  description = "Run as this service account (a dedicated identity the workspace created); null leaves it."
  type        = string
  default     = null
}
