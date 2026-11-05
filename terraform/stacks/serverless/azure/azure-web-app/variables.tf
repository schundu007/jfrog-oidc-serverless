variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type = string
}

variable "workload" {
  description = "Workload name: its resource group, identity, plan and web app are named after it."
  type        = string
}

variable "os" {
  description = "App Service plan OS: linux or windows."
  type        = string
  default     = "linux"
  validation {
    condition     = contains(["linux", "windows"], var.os)
    error_message = "os must be linux or windows."
  }
}

variable "runtime" {
  description = "Code platform: python, java, dotnet, node or php. Python and PHP run on Linux only."
  type        = string
  default     = "python"
  validation {
    condition     = contains(["python", "java", "dotnet", "node", "php"], var.runtime)
    error_message = "runtime must be python, java, dotnet, node or php."
  }
}

variable "runtime_version" {
  description = "Language version (python 3.13, java 21, dotnet 10.0, node 24-lts, php 8.4). Empty: that default."
  type        = string
  default     = ""
}

variable "sku" {
  description = "App Service plan SKU."
  type        = string
  default     = "B1"
}

variable "slot" {
  description = "Name of a deployment slot to create next to production (e.g. staging), with the same identity. Needs a Standard (S1) or higher plan. Empty: none."
  type        = string
  default     = ""
  validation {
    condition     = can(regex("^([a-z0-9][a-z0-9-]{0,38})?$", var.slot)) && var.slot != "production"
    error_message = "slot must be lowercase letters, digits and hyphens (not production), or empty."
  }
}

variable "deployer_principal_id" {
  description = "Object ID of the build pipeline's identity (deploys code into the app's resource group). Empty: none."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
