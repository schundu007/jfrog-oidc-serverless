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

variable "azure_location" {
  description = "Region of the pipeline identity's resource group, identities and state storage."
  type        = string
}

variable "azure_identity" {
  description = "Build identity behind the ADO service connections and the resource group it lives in; the pipeline identity creates both."
  type = object({
    name           = string
    resource_group = string
  })
}


variable "azure_console_operators" {
  description = "Emails of the people who run the Infrastructure page: read/write the Azure Terraform state (the pipelines' setup identity already can)."
  type        = list(string)
  default     = []
}

variable "ado_project" {
  description = "Azure DevOps project whose pipelines read this stack's variable group."
  type        = string
}

variable "pipeline_environment" {
  description = "Environment name the pipelines pass (parameters.environment); names the variable group."
  type        = string
  default     = "test"
}
