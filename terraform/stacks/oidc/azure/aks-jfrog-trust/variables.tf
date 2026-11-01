variable "jfrog_url" {
  description = "JFrog platform origin, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only: https, no path, no trailing slash."
  }
}

variable "azure_subscription_id" {
  description = "Subscription that holds the AKS cluster."
  type        = string
}

variable "cluster_name" {
  description = "The AKS cluster (aks-cluster's name input)."
  type        = string
}

variable "cluster_resource_group" {
  description = "The cluster's resource group."
  type        = string
}

variable "mapped_user" {
  description = "JFrog user the nodes pull as (an existing user with read on the repositories)."
  type        = string
}

variable "namespace" {
  description = "Namespace of the credential provider and its service account."
  type        = string
  default     = "jfrog"
}

variable "service_account" {
  description = "Service account whose token the kubelet exchanges for a JFrog token."
  type        = string
  default     = "jfrog-provider-sa"
}

variable "token_scope" {
  type    = string
  default = "applied-permissions/user"
}

variable "token_audience" {
  type    = string
  default = "*@*"
}

variable "token_expires_in" {
  description = "Seconds the JFrog token lives."
  type        = number
  default     = 3600
}

variable "match_images" {
  description = "Image patterns the kubelet asks the provider about. Empty: the JFrog host."
  type        = list(string)
  default     = []
}

variable "cache_duration" {
  type    = string
  default = "30m"
}

variable "node_label" {
  description = "Node label (value \"true\") that selects the nodes the provider is installed on: aks-cluster's node_labels."
  type        = string
  default     = "credentialsProviderEnabled"
}

variable "chart_version" {
  description = "jfrog/jfrog-credential-provider chart version."
  type        = string
  default     = "1.4.0"
}
