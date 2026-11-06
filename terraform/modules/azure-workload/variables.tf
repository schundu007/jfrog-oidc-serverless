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
  description = "Unused: the resource group (made by azure-pipeline-identity) sets the location."
  type        = string
  default     = ""
}

variable "identity_type" {
  description = "Runtime identity: SystemAssigned (made and deleted with the workload) or UserAssigned (id-<workload>, made by azure-pipeline-identity)."
  type        = string
  default     = "SystemAssigned"
  validation {
    condition     = contains(["SystemAssigned", "UserAssigned"], var.identity_type)
    error_message = "identity_type must be SystemAssigned or UserAssigned."
  }
}


variable "registry" {
  description = "Create a container registry the workload pulls from with its identity."
  type        = bool
  default     = false
}

variable "acr_name" {
  description = "Registry name (global). Empty: derived from the workload and subscription."
  type        = string
  default     = ""
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
