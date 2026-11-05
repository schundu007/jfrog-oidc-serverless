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

variable "deployer_principal_id" {
  description = "Object ID of the build pipeline's identity (rolls out the workload in its resource group). Empty: none."
  type        = string
  default     = ""
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
