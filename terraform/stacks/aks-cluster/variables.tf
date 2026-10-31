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
