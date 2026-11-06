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

variable "identity_type" {
  description = "Runtime identity: UserAssigned (id-<workload>, made by azure-pipeline-identity with the host storage roles before the app starts; the default) or SystemAssigned (made with the app; its storage roles are granted after it exists, through azure-pipeline-identity's workloads.runtime_principal_id). Same value as in azure-pipeline-identity's workloads."
  type        = string
  default     = "UserAssigned"
  validation {
    condition     = contains(["SystemAssigned", "UserAssigned"], var.identity_type)
    error_message = "identity_type must be SystemAssigned or UserAssigned."
  }
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
