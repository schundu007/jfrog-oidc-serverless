# This cloud's own ADO service connections (workload identity federation), one per role,
# backed by the managed identities in main.tf. Their subjects feed the federated credentials
# and the JFrog mappings, so nothing about them is entered by hand.

resource "azuredevops_serviceendpoint_azurerm" "ado" {
  for_each = local.azure_identity

  project_id                             = data.azuredevops_project.this.id
  service_endpoint_name                  = "jfrog-oidc-azure-${each.key}"
  description                            = "Azure ${each.key} pipelines. Written by terraform/stacks/azure-foundation."
  service_endpoint_authentication_scheme = "WorkloadIdentityFederation"
  azurerm_spn_tenantid                   = var.azure_tenant_id
  azurerm_subscription_id                = var.azure_subscription_id
  azurerm_subscription_name              = data.azurerm_subscription.this.display_name
  credentials {
    serviceprincipalid = each.value.client_id
  }
}

# ADO refuses to delete a connection while Entra still lists a federated credential for its
# subject, and Entra takes a while to forget a deleted one: destroy waits here between the two.
resource "time_sleep" "credential_propagation" {
  depends_on       = [azuredevops_serviceendpoint_azurerm.ado]
  destroy_duration = "120s"
}

# The azure-* pipelines may use them without a manual "Permit" on first run.
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
