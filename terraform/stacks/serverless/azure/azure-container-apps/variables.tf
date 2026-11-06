variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type = string
}

variable "workload" {
  description = "Workload name: resource group, identity and job are named after it."
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

variable "identity_type" {
  description = "Runtime identity: UserAssigned (id-<workload>, made by azure-pipeline-identity with AcrPull before the first image pull; the default) or SystemAssigned (made with the workload; AcrPull is granted after it exists, so the first revision fails to pull until then). Same value as in azure-pipeline-identity's workloads."
  type        = string
  default     = "UserAssigned"
  validation {
    condition     = contains(["SystemAssigned", "UserAssigned"], var.identity_type)
    error_message = "identity_type must be SystemAssigned or UserAssigned."
  }
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

variable "tags" {
  type    = map(string)
  default = {}
}
