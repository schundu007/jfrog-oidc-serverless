variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type    = string
  default = "westus2"
}

variable "workload" {
  description = "Workload name: its resource group, identity, plan and web app are named after it."
  type        = string
  default     = "app-jfrog-demo"
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
  description = "Code platform: python, java or dotnet. Python runs on Linux only."
  type        = string
  default     = "python"
  validation {
    condition     = contains(["python", "java", "dotnet"], var.runtime)
    error_message = "runtime must be python, java or dotnet."
  }
}

variable "runtime_version" {
  description = "Language version (python 3.12, java 21, dotnet 8.0). Empty: that default."
  type        = string
  default     = ""
}

variable "sku" {
  description = "App Service plan SKU."
  type        = string
  default     = "B1"
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
