variable "azure_subscription_id" {
  type = string
}

variable "azure_location" {
  type = string
}

variable "workload" {
  description = "Workload name: its resource group, identity and container app are named after it."
  type        = string
}

variable "identity_type" {
  description = "Runtime identity: UserAssigned (id-<workload>, made by azure-pipeline-identity with AcrPull before the first image pull; the default) or SystemAssigned (made with the workload; AcrPull is granted after it exists, so the first revision fails to pull until then). Same value as in azure-pipeline-identity's workloads."
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

variable "deploy_app" {
  description = "Create the container app (after the first image is pushed)."
  type        = bool
  default     = false
}

variable "app_image" {
  description = "<repository>:<tag> in the stack's registry the app is created with; later rollouts come from the build pipeline."
  type        = string
  default     = ""
}
