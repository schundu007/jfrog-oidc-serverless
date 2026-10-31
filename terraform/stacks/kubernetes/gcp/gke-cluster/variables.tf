variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  description = "A zonal cluster keeps the test cheap (one zone, one node)."
  type        = string
  default     = "us-central1-a"
}

variable "name" {
  type    = string
  default = "gke-jfrog-test"
}

variable "node_count" {
  type    = number
  default = 1
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}
