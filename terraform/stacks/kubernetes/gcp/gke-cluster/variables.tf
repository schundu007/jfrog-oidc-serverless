variable "gcp_project_id" {
  type = string
}

variable "gcp_region" {
  type = string
}

variable "zone" {
  description = "A zonal cluster keeps the test cheap (one zone, one node)."
  type        = string
}

variable "name" {
  type = string
}

variable "node_count" {
  type    = number
  default = 1
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}
