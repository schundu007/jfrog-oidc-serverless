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
  description = "JFrog OIDC integration for Google-issued tokens."
  type        = string
  default     = "gcp-cloudrun"
}

variable "jfrog_audience" {
  description = "Audience the workload requests on its Google ID token."
  type        = string
  default     = "jfrog-gcp"
}

variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "workload" {
  description = "Workload name: service account, Cloud Run job and JFrog mapping."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.workload))
    error_message = "workload must be 6-30 chars: lowercase letters, digits, hyphens."
  }
}

variable "image_repository" {
  description = "Artifact Registry repository for workload images."
  type        = string
  default     = "platform"
}

variable "deployer_service_account" {
  description = "Service account of the build pipeline (gets actAs on the workload account). Empty: none."
  type        = string
  default     = ""
}

variable "deploy_job" {
  description = "Create the Cloud Run job (after the first image is pushed)."
  type        = bool
  default     = false
}

variable "job_image" {
  description = "Image the job is created with; later rollouts come from the build pipeline."
  type        = string
  default     = ""
  validation {
    condition     = var.job_image == "" || can(regex("^[a-z0-9.-]+-docker\\.pkg\\.dev/", var.job_image))
    error_message = "job_image must be an Artifact Registry image (<region>-docker.pkg.dev/...)."
  }
}

variable "artifact_path" {
  description = "<repo>/<path> the job reads from JFrog."
  type        = string
  default     = ""
}
