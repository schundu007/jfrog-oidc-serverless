variable "workload" {
  description = "Workload name: every resource of this workload is named after it."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}[a-z0-9]$", var.workload))
    error_message = "workload must be 4-32 chars: lowercase letters, digits, hyphens."
  }
}

variable "azure_subscription_id" {
  type = string
}

variable "location" {
  type = string
}

variable "deployer_principal_id" {
  type    = string
  default = ""
}

variable "registry" {
  description = "Create a container registry the workload pulls from with its identity."
  type        = bool
  default     = false
}

variable "acr_sku" {
  type    = string
  default = "Standard"
}

variable "container_apps_environment" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "storage" {
  description = "Create the Functions host storage account (identity access, no keys)."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
