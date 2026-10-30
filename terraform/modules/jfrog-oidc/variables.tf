variable "gcp" {
  description = <<-EOT
    Trust Google-signed ID tokens (Cloud Run services, jobs and functions).
    audience is the string workloads request from the metadata server; it lands
    in the token's aud claim. Set to null to skip the Google integration.
  EOT
  type = object({
    provider_name = optional(string, "gcp-cloudrun")
    audience      = optional(string, "jfrog-gcp")
  })
  default = null
}

variable "azure" {
  description = <<-EOT
    Trust Entra-issued managed-identity tokens (Container Apps, Functions).
    audience_app_id is the client ID of the Entra app registration that acts as
    the audience (output audience_app_id of the azure module). With
    requestedAccessTokenVersion = 2 the token's iss is the v2.0 endpoint and its
    aud is this client ID. Set to null to skip the Azure integration.
  EOT
  type = object({
    provider_name   = optional(string, "azure-entra")
    tenant_id       = string
    audience_app_id = string
    provider_type   = optional(string, "Azure")
  })
  default = null
}

variable "aws" {
  description = <<-EOT
    Trust JWTs from STS GetWebIdentityToken (IAM outbound identity federation).
    issuer is the account's issuer (aws iam get-outbound-web-identity-federation-info:
    https://<id>.tokens.sts.global.api.aws); audience is what workloads request
    (JFROG_OIDC_AUDIENCE). Set to null to skip.
  EOT
  type = object({
    provider_name = string
    issuer        = string
    audience      = optional(string, "jfrog-aws")
  })
  default = null

  validation {
    condition     = var.aws == null || can(regex("^https://[^/]+$", var.aws.issuer))
    error_message = "aws.issuer must be the bare https issuer URL, with no path or trailing slash."
  }
}

variable "openshift" {
  description = <<-EOT
    Trust projected service-account tokens from one Kubernetes cluster (OpenShift
    Serverless or any cluster with Knative). issuer is the cluster's
    service-account issuer; its /.well-known/openid-configuration and JWKS must be
    reachable from JFrog. Set to null to skip.
  EOT
  type = object({
    provider_name = string
    issuer        = string
    audience      = optional(string, "jfrog-k8s")
  })
  default = null

  validation {
    condition     = var.openshift == null || startswith(var.openshift.issuer, "https://")
    error_message = "openshift.issuer must be an https URL that JFrog can reach."
  }
}

variable "project_key" {
  description = "Optional JFrog project key for the integrations. Mappings ignore it (per provider docs); scope access through groups."
  type        = string
  default     = null
}

variable "workloads" {
  description = <<-EOT
    One identity mapping per workload identity. Key = mapping name.

    cloud       "gcp", "azure", "aws" or "openshift".
    subject     GCP: the service account's unique_id (numeric, matches the sub claim).
                Azure: the managed identity's principal (object) ID (matches oid).
                AWS: the IAM role ARN (matches sub).
                OpenShift/Knative: system:serviceaccount:<namespace>:<name> (matches sub).
    username    Optional. Only set it on image-pull (rotator) mappings, where the
                platform needs a fixed registry username. JFrog adds
                applied-permissions/user and that user's Auto Join groups to the
                token when a username is set, so the named user must have no
                permissions beyond read. Runtime mappings leave it null and get a
                purely group-scoped token.
    groups      JFrog groups whose permissions the token gets. Keep these read-only
                for runtime and pull identities.
    expires_in  Token lifetime in seconds. See README for TTL vs rotation math.
    priority    Lower number wins when several mappings match.
  EOT
  type = map(object({
    cloud       = string
    subject     = string
    username    = optional(string)
    groups      = list(string)
    expires_in  = optional(number, 3600)
    priority    = optional(number, 10)
    description = optional(string, "")
  }))

  validation {
    condition     = alltrue([for w in values(var.workloads) : contains(["gcp", "azure", "aws", "openshift"], w.cloud)])
    error_message = "Each workload's cloud must be \"gcp\", \"azure\", \"aws\" or \"openshift\"."
  }

  validation {
    condition     = alltrue([for w in values(var.workloads) : length(w.groups) > 0])
    error_message = "Each workload needs at least one JFrog group; group-scoped tokens keep permissions out of the mapping."
  }

  validation {
    condition     = alltrue([for w in values(var.workloads) : w.expires_in >= 300 && w.expires_in <= 86400])
    error_message = "expires_in must be between 300 seconds and 24 hours (JFrog's cap for identity-mapping tokens)."
  }

  # Mapping rules carried over from the AKS pilot (docs/aks-pilot-lessons.md):
  # exact subjects only, one mapping per identity, runtime tokens at most 1h.
  validation {
    condition     = alltrue([for w in values(var.workloads) : length(trimspace(w.subject)) > 0 && !strcontains(w.subject, "*")])
    error_message = "Each workload's subject must be the exact SA unique ID or principal ID: not empty, no wildcards."
  }

  validation {
    condition     = length(distinct([for w in values(var.workloads) : "${w.cloud}/${w.subject}"])) == length(var.workloads)
    error_message = "One mapping per identity: two workloads share a cloud and subject, so priority alone would decide which token it gets."
  }

  validation {
    condition     = alltrue([for w in values(var.workloads) : w.username != null || w.expires_in <= 3600])
    error_message = "Runtime mappings (no username) are capped at 3600 seconds: the client refreshes before expiry, so longer only widens the leak window. Only pull (rotator) mappings may run longer."
  }
}
