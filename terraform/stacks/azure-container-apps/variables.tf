variable "jfrog_url" {
  description = "JFrog platform origin, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only: https, no path, no trailing slash."
  }
}

variable "jfrog_reader_group" {
  description = "Existing JFrog group with read permission on the repos the workload uses."
  type        = string
}

variable "jfrog_provider_name" {
  description = "JFrog OIDC integration for Entra-issued tokens."
  type        = string
  default     = "azure-entra"
}

variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type    = string
  default = "westus2"
}

variable "workload" {
  description = "Workload name: resource group, identity, job and JFrog mapping are named after it."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,17}$", var.workload))
    error_message = "workload must be 2-18 chars: lowercase letters, digits, hyphens."
  }
}

variable "acr_name" {
  description = "Container registry name (global). Empty: derived from workload and subscription."
  type        = string
  default     = ""
}

variable "acr_sku" {
  type    = string
  default = "Standard"
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "deployer_principal_id" {
  description = "Object ID of the build pipeline's identity (AcrPush and Contributor on the workload's resource group). Empty: none."
  type        = string
  default     = ""
}

variable "deploy_job" {
  description = "Create the Container Apps job (after the first image is pushed)."
  type        = bool
  default     = false
}

variable "job_image" {
  description = "<repository>:<tag> in the stack's ACR the job is created with; later rollouts come from the build pipeline."
  type        = string
  default     = ""
}

variable "artifact_path" {
  description = "<repo>/<path> the job reads from JFrog."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
