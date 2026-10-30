variable "project_id" {
  type = string
}

variable "region" {
  description = "Region for the Artifact Registry remote repos, rotator job and scheduler."
  type        = string
}

variable "name_prefix" {
  type    = string
  default = "jfrog"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,13}$", var.name_prefix))
    error_message = "name_prefix: 2-14 chars, lowercase letters, digits, hyphens (service account IDs are capped at 30)."
  }
}

variable "enable_apis" {
  type    = bool
  default = true
}

variable "jfrog_url" {
  description = "JFrog platform URL, e.g. https://acme.jfrog.io"
  type        = string
  validation {
    # Origin only: the client and rotator append /access/... and /artifactory/...
    # themselves (docs/aks-pilot-lessons.md: the doubled-URL failure).
    condition     = can(regex("^https://[a-z0-9.-]+$", var.jfrog_url))
    error_message = "jfrog_url must be the platform origin only, e.g. https://acme.jfrog.io: https, no path, no trailing slash."
  }
}

variable "jfrog_provider_name" {
  description = "JFrog OIDC integration name for Google tokens (jfrog-oidc module output gcp_provider_name)."
  type        = string
}

variable "jfrog_audience" {
  description = "Audience the rotator requests from the metadata server (jfrog-oidc module output gcp_audience)."
  type        = string
}

variable "registry_username" {
  description = "Initial upstream username; the rotator overwrites it with the username JFrog returns, which is the mapping's token_spec.username."
  type        = string
}

variable "remote_repositories" {
  description = <<-EOT
    Artifact Registry remote repositories that proxy JFrog. Key = repository ID.
      format        DOCKER | NPM | PYTHON | MAVEN
      upstream_uri  DOCKER: the repo's dedicated host, https://acme-<repo-key>.jfrog.io
                            (images then resolve as <ar-repo>/<image>). Do not use the
                            platform host: that is the repository-path method, which
                            failed for local repos on the AKS pilot's instance.
                    NPM:    https://acme.jfrog.io/artifactory/api/npm/npm-virtual
                    PYTHON: https://acme.jfrog.io/artifactory/api/pypi/pypi-virtual
                    MAVEN:  https://acme.jfrog.io/artifactory/maven-virtual
  EOT
  type = map(object({
    format       = string
    upstream_uri = string
    description  = optional(string)
  }))
  validation {
    condition     = alltrue([for r in values(var.remote_repositories) : contains(["DOCKER", "NPM", "PYTHON", "MAVEN"], r.format)])
    error_message = "format must be DOCKER, NPM, PYTHON or MAVEN."
  }
  validation {
    condition     = alltrue([for r in values(var.remote_repositories) : r.format != "DOCKER" || can(regex("^https://[a-z0-9.-]+$", r.upstream_uri))])
    error_message = "A DOCKER upstream_uri is a bare https host (the repo's own subdomain), with no path or trailing slash."
  }
}

variable "reader_members" {
  description = <<-EOT
    Principals that read from the remote repos: the Cloud Build service account
    used by Cloud Run functions builds, runtime service accounts, and the Cloud Run
    service agent of other projects that deploy these images.
    e.g. ["serviceAccount:123-compute@developer.gserviceaccount.com"]
  EOT
  type        = list(string)
  default     = []
}

variable "rotator_image" {
  description = "Rotator image. Host it in a standard AR repo, not behind the JFrog remote it refreshes."
  type        = string
}

variable "rotation_schedule" {
  description = "Cron (UTC) for the rotator. Keep the interval well under the mapping's expires_in."
  type        = string
  default     = "17 * * * *"
}

variable "min_ttl_seconds" {
  description = "Rotator fails if JFrog issues a token with less lifetime than this. Matches rotate.py's default; size the mapping TTL from the README's TTL table, not from this floor."
  type        = number
  default     = 1800
}

variable "keep_versions" {
  description = "Enabled secret versions to keep (current + previous)."
  type        = number
  default     = 2
}

variable "notification_channels" {
  description = "Monitoring notification channel IDs. When non-empty, alert if no successful rotation within alert_after_seconds."
  type        = list(string)
  default     = []
}

variable "alert_after_seconds" {
  description = "Alert when no rotation has succeeded for this long. Must be less than the token TTL."
  type        = number
  default     = 10800
}
