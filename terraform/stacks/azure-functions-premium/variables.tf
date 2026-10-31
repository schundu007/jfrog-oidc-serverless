variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type    = string
  default = "westus2"
}

variable "workload" {
  description = "Workload name: its resource group, identity and plan and function app are named after it."
  type        = string
  default     = "func-prem-jfrog-demo"
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

variable "plan_sku" {
  description = "Elastic Premium (EP1-EP3) or Dedicated (B1, S1, P1v3, ...) plan."
  type        = string
  default     = "EP1"
}
