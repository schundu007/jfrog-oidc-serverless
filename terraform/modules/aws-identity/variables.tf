variable "audience" {
  description = "The only audience the roles may request from STS GetWebIdentityToken (JFROG_OIDC_AUDIENCE)."
  type        = string
  default     = "jfrog-aws"
}

variable "name_prefix" {
  description = "Prefix of the inline policy name on each role."
  type        = string
  default     = "jfrog"
}

variable "existing_roles" {
  description = <<-EOT
    Roles the workloads already run as: <workload> => role name. Each gets an
    inline policy that allows sts:GetWebIdentityToken for var.audience only.
  EOT
  type        = map(string)
  default     = {}
}

variable "dedicated_roles" {
  description = <<-EOT
    Workloads with no role of their own, or one shared with other workloads:
    <workload> => { name, kind }. A role is created that only the workload's
    service can assume (kind aws_lambda, aws_ecs_task or aws_app_runner) and Wire
    switches the workload to it. Lambda roles also get basic logging.
  EOT
  type = map(object({
    name = string
    kind = string
  }))
  default = {}

  validation {
    condition     = alltrue([for r in values(var.dedicated_roles) : contains(["aws_lambda", "aws_ecs_task", "aws_app_runner"], r.kind)])
    error_message = "dedicated_roles kind must be aws_lambda, aws_ecs_task or aws_app_runner."
  }
}

variable "max_token_seconds" {
  description = "Longest JWT the roles may request (sts:DurationSeconds). The client asks for 900."
  type        = number
  default     = 3600
}

variable "enable_outbound_federation" {
  description = <<-EOT
    Also enable IAM outbound identity federation for the account
    (aws_iam_outbound_web_identity_federation). Account-wide: destroying it turns
    the feature off for every workload in the account. Off by default; the
    console's Preflight asks you to run aws iam enable-outbound-web-identity-federation once.
  EOT
  type        = bool
  default     = false
}
