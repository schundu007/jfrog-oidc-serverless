variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type = string
}

variable "workload" {
  description = "Workload name: its service account and App Engine service are named after it."
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

variable "create_application" {
  description = "Create the project's App Engine application. One per project; its region can never be changed and it cannot be deleted (destroy leaves it in place). Leave off when the project already has one."
  type        = bool
  default     = false
}

variable "location_id" {
  description = "App Engine location for a new application (for example us-central, europe-west). Empty: gcp_region. Used only with create_application."
  type        = string
  default     = ""
}
