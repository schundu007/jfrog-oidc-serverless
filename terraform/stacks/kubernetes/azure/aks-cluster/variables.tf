variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type    = string
  default = "westus2"
}

variable "name" {
  description = "Cluster name; the resource group is rg-<name>."
  type        = string
  default     = "aks-jfrog-test"
}

variable "node_count" {
  type    = number
  default = 1
}

variable "node_size" {
  type    = string
  default = "Standard_D2s_v3"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "log_retention_days" {
  description = "Days Log Analytics keeps the cluster's logs."
  type        = number
  default     = 30
}

variable "argocd_enabled" {
  description = "Install the Argo CD hub (gitops/argocd/values.yaml) on the cluster."
  type        = bool
  default     = true
}

variable "argocd_identity" {
  description = "Argo CD's account, from azure-foundation's argocd_identity output (its identity name and resource group)."
  type = object({
    name           = string
    resource_group = string
  })
}

variable "argocd_chart_version" {
  description = "argo/argo-cd chart version."
  type        = string
  default     = "10.11.6"
}
