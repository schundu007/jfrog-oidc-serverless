# JFrog trust for Azure DevOps pipelines: no cloud resources here.
# Each job's service connection token (Entra-issued: iss https://login.microsoftonline.com/<tenant>/v2.0,
# sub unique per connection) is exchanged at JFrog:
#   build -> reader group (read + remote cache), setup -> admin (Terraform)

resource "platform_oidc_configuration" "ado" {
  name          = "ado-${var.ado_org}"
  description   = "Azure DevOps ${var.ado_org} pipelines (Entra-issued service connection tokens)"
  issuer_url    = "https://login.microsoftonline.com/${var.ado_tenant_id}/v2.0"
  provider_type = "generic"
  audience      = var.ado_token_audience
}

resource "platform_oidc_identity_mapping" "build" {
  name          = "ado-build"
  description   = "ADO build/deploy pipelines (${var.connections.build.name})"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 1
  claims_json   = jsonencode({ sub = var.connections.build.subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/groups:\"${var.jfrog_reader_group}\""
    audience   = "jfrt@*"
    expires_in = 3600
  }
}

resource "platform_oidc_identity_mapping" "setup" {
  name          = "ado-setup"
  description   = "ADO setup pipelines: Terraform admin (${var.connections.setup.name})"
  provider_name = platform_oidc_configuration.ado.name
  priority      = 2
  claims_json   = jsonencode({ sub = var.connections.setup.subject, aud = var.ado_token_audience })
  token_spec = {
    scope      = "applied-permissions/admin"
    username   = "ado-setup"
    audience   = "jfrt@* jfac@*"
    expires_in = 3600
  }
}
