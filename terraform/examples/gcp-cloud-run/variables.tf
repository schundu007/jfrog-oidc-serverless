variable "jfrog_url" {
  description = "JFrog platform origin, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only, e.g. https://acme.jfrog.io: https, no path, no trailing slash."
  }
}

variable "jfrog_reader_group" {
  description = "Existing JFrog group with read-only permission on the repos the workloads use."
  type        = string
}

variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "deploy_demo" {
  description = "Second apply: deploy the demo job (build samples/cloud-run-job first)."
  type        = bool
  default     = false
}

variable "demo_image" {
  description = "Artifact Registry image built from samples/cloud-run-job (needed when deploy_demo = true)."
  type        = string
  default     = ""
}

variable "demo_artifact" {
  description = "<repo>/<path> the demo job downloads."
  type        = string
  default     = "demo-generic/hello.txt"
}
