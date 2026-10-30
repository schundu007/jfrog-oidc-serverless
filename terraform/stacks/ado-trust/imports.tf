# The build connection's credential was created by hand before this module existed.
import {
  to = azurerm_federated_identity_credential.ado["build"]
  id = "/subscriptions/${var.azure_subscription_id}/resourceGroups/${var.azure_identity.resource_group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/${var.azure_identity.name}/federatedIdentityCredentials/ado-${var.connections.build.name}"
}
