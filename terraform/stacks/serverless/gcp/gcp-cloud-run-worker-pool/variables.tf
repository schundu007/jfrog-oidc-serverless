variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type = string
}

variable "workload" {
  description = "Workload name: its service account and Cloud Run worker pool are named after it."
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

variable "image_repository" {
  description = "Artifact Registry repository for workload images (created by the pipeline identity stack)."
  type        = string
  default     = "platform"
}

variable "deploy_worker_pool" {
  description = "Create the Cloud Run worker pool (after the first image is pushed)."
  type        = bool
  default     = false
}

variable "worker_pool_image" {
  description = "Image the worker pool is created with; later rollouts come from the build pipeline."
  type        = string
  default     = ""
  validation {
    condition     = var.worker_pool_image == "" || can(regex("^[a-z0-9.-]+-docker\\.pkg\\.dev/", var.worker_pool_image))
    error_message = "worker_pool_image must be an Artifact Registry image (<region>-docker.pkg.dev/...)."
  }
}

variable "instances" {
  description = "Instances the worker pool keeps running (manual scaling)."
  type        = number
  default     = 1
  validation {
    condition     = var.instances >= 0 && var.instances <= 10
    error_message = "instances must be 0-10."
  }
}
