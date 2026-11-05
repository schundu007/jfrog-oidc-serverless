variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type = string
}

variable "workload" {
  description = "Workload name: its resource group, identity and plan and function app are named after it."
  type        = string
}

variable "deployer_principal_id" {
  description = "Object ID of the build pipeline's identity (rolls out the workload in its resource group). Empty: none."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
