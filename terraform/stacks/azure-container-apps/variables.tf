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

variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type    = string
  default = "westus2"
}

variable "deploy_demo" {
  description = "Second apply: deploy the demo job (build samples/cloud-run-job into ACR first)."
  type        = bool
  default     = false
}

variable "demo_image" {
  description = "Repository:tag in the example's ACR, built from samples/cloud-run-job."
  type        = string
  default     = "jfrog-oidc-demo:0.1.0"
}

variable "demo_artifact" {
  description = "<repo>/<path> the demo job downloads."
  type        = string
  default     = "demo-generic/hello.txt"
}
