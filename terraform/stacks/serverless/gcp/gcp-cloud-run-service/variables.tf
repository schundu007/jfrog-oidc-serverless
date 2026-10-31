variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "workload" {
  description = "Workload name: its service account and Cloud Run service are named after it."
  type        = string
  default     = "cr-svc-jfrog"
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

variable "image_repository" {
  description = "Artifact Registry repository for workload images (created by the foundation)."
  type        = string
  default     = "platform"
}

variable "deploy_service" {
  description = "Create the Cloud Run service (after the first image is pushed)."
  type        = bool
  default     = false
}

variable "service_image" {
  description = "Image the service is created with; later rollouts come from the build pipeline."
  type        = string
  default     = ""
  validation {
    condition     = var.service_image == "" || can(regex("^[a-z0-9.-]+-docker\\.pkg\\.dev/", var.service_image))
    error_message = "service_image must be an Artifact Registry image (<region>-docker.pkg.dev/...)."
  }
}
