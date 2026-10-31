# JFrog trust for this cloud's ADO pipelines only (its own integration and service connections):
#   build -> reader group (read + remote cache), setup -> admin (Terraform)

resource "platform_oidc_configuration" "ado" {
  name          = "ado-${var.ado_org}-gcp"
  description   = "Azure DevOps ${var.ado_org} GCP pipelines (Entra-issued service connection tokens)"
  issuer_url    = local.issuer
  provider_type = "generic"
  audience      = var.ado_token_audience
}

resource "platform_oidc_identity_mapping" "build" {
  name          = "ado-build"
  description   = "ADO build/deploy pipelines (${local.connections.build.name})"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 1
  claims_json   = jsonencode({ sub = local.connections.build.subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/groups:\"${var.jfrog_reader_group}\""
    audience   = "jfrt@*"
    expires_in = 3600
  }
}

resource "platform_oidc_identity_mapping" "setup" {
  name          = "ado-setup"
  description   = "ADO setup pipelines: Terraform admin (${local.connections.setup.name})"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 2
  claims_json   = jsonencode({ sub = local.connections.setup.subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/admin"
    username   = "ado-setup"
    audience   = "jfrt@* jfac@*"
    expires_in = 3600
  }
}
