variable "ado_org" {
  description = "Azure DevOps organization name (used in resource names)."
  type        = string
}

variable "azure_tenant_id" {
  description = "Entra tenant that issues the ADO service connections' tokens."
  type        = string
}

variable "azure_subscription_id" {
  type = string
}

variable "azure_identity" {
  description = "Managed identity behind the ADO service connections."
  type = object({
    name           = string
    resource_group = string
  })
}

variable "connections" {
  description = <<-EOT
    ADO service connections (workload identity federation): build and setup.
    subject is the connection's workloadIdentityFederationSubject.
  EOT
  type = map(object({
    name    = string
    subject = string
  }))
  validation {
    condition     = toset(keys(var.connections)) == toset(["build", "setup"])
    error_message = "connections needs exactly the keys build and setup."
  }
}

variable "azure_console_operators" {
  description = "Entra object IDs of people who run the Infrastructure page: read/write the Azure Terraform state (the pipelines' setup identity already can)."
  type        = list(string)
  default     = []
}
