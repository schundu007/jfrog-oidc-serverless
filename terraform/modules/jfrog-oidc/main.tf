# JFrog side of the trust: one OIDC integration per cloud issuer, and one
# identity mapping per workload identity. A workload presents its platform-signed
# token to /access/api/v1/oidc/token with provider_name = the integration name;
# JFrog validates the signature against the issuer's JWKS, matches claims_json,
# and returns a short-lived access token described by token_spec.

locals {
  gcp_issuer   = "https://accounts.google.com"
  azure_issuer = var.azure == null ? null : "https://login.microsoftonline.com/${var.azure.tenant_id}/v2.0"

  # Claims every token must carry. JFrog only validates claims listed here, so
  # pin issuer-specific identity (sub/oid), tenant and audience explicitly.
  claims = {
    for name, w in var.workloads : name => (
      w.cloud == "gcp" ? {
        sub = w.subject
        aud = var.gcp.audience
        } : {
        oid = w.subject
        tid = var.azure.tenant_id
        aud = var.azure.audience_app_id
      }
    )
  }

  provider_for = {
    gcp   = var.gcp == null ? null : var.gcp.provider_name
    azure = var.azure == null ? null : var.azure.provider_name
  }
}

resource "platform_oidc_configuration" "gcp" {
  count = var.gcp == null ? 0 : 1

  name          = var.gcp.provider_name
  description   = "Google-signed ID tokens from Cloud Run services, jobs and functions"
  issuer_url    = local.gcp_issuer
  provider_type = "generic"
  audience      = var.gcp.audience
  project_key   = var.project_key
}

resource "platform_oidc_configuration" "azure" {
  count = var.azure == null ? 0 : 1

  name          = var.azure.provider_name
  description   = "Entra managed-identity tokens from Container Apps and Functions"
  issuer_url    = local.azure_issuer
  provider_type = var.azure.provider_type
  audience      = var.azure.audience_app_id
  project_key   = var.project_key
}

resource "platform_oidc_identity_mapping" "workload" {
  for_each = var.workloads

  name          = each.key
  description   = coalesce(each.value.description, "${each.value.cloud} workload ${each.key}")
  provider_name = local.provider_for[each.value.cloud]
  priority      = each.value.priority

  claims_json = jsonencode(local.claims[each.key])

  token_spec = {
    username   = each.value.username
    scope      = "applied-permissions/groups:${join(",", [for g in each.value.groups : "\"${g}\""])}"
    audience   = "jfrt@*" # Artifactory only
    expires_in = each.value.expires_in
  }

  depends_on = [
    platform_oidc_configuration.gcp,
    platform_oidc_configuration.azure,
  ]

  lifecycle {
    precondition {
      condition     = local.provider_for[each.value.cloud] != null
      error_message = "Workload ${each.key} targets ${each.value.cloud} but that integration is not configured (var.${each.value.cloud} is null)."
    }
  }
}
