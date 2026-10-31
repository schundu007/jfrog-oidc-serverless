# This cloud's own ADO service connections, one per role. They only issue the job's OIDC
# token (System.OidcRequestUri): nothing signs in to Azure with them, so the Azure fields
# ADO requires on this connection type are placeholders, not an Azure identity or
# subscription. The pipelines reference them from a skipped task, which is all ADO needs
# before it issues the token. Their subjects feed the WIF provider and the JFrog mappings.

locals {
  not_azure = "00000000-0000-0000-0000-000000000000"
}

resource "azuredevops_serviceendpoint_azurerm" "ado" {
  for_each = toset(["build", "setup"])

  project_id                             = data.azuredevops_project.this.id
  service_endpoint_name                  = "jfrog-oidc-gcp-${each.key}"
  description                            = "GCP ${each.key} pipelines: OIDC token issuer only, no Azure access. Written by terraform/stacks/gcp-foundation."
  service_endpoint_authentication_scheme = "WorkloadIdentityFederation"
  azurerm_spn_tenantid                   = var.ado_tenant_id
  azurerm_subscription_id                = local.not_azure
  azurerm_subscription_name              = "none (token issuer only)"
  credentials {
    serviceprincipalid = local.not_azure
  }
}

# The gcp-* pipelines may use them without a manual "Permit" on first run.
resource "azuredevops_pipeline_authorization" "ado" {
  for_each = azuredevops_serviceendpoint_azurerm.ado

  project_id  = data.azuredevops_project.this.id
  resource_id = each.value.id
  type        = "endpoint"
}

locals {
  connections = {
    for k, c in azuredevops_serviceendpoint_azurerm.ado :
    k => { name = c.service_endpoint_name, id = c.id, subject = c.workload_identity_federation_subject }
  }
}
