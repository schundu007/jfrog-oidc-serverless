variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type = string
}

variable "workload" {
  description = "Workload name: its service account and function are named after it."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.workload))
    error_message = "workload must be 6-30 chars: lowercase letters, digits, hyphens."
  }
}

variable "deployer_service_account" {
  description = "Service account of the build pipeline (gets actAs on the workload account). Empty: none."
  type        = string
  default     = ""
}

variable "deploy_function" {
  description = "Create the function (after the first source package is uploaded)."
  type        = bool
  default     = false
}

variable "source_object" {
  description = "Source package (object name in the stack's source bucket) the function is created with; later deploys come from the build pipeline."
  type        = string
  default     = ""
}
