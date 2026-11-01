# Argo CD's own account: the identity the AKS Argo CD hub's repo-server signs in as to read the GitOps
# repo in Azure DevOps (Basic access, project Readers). It lives here, with the cloud's other ADO trust,
# so it survives cluster teardowns and the pipelines' setup identity never needs ADO admin rights.
# The aks-cluster stack only federates it to each new cluster's OIDC issuer (Azure RBAC alone).
# No secret anywhere: the repo-server gets Entra tokens through Azure Workload Identity.

resource "azurerm_user_assigned_identity" "argocd" {
  name                = "${var.azure_identity.name}-argocd"
  location            = azurerm_resource_group.ado.location
  resource_group_name = azurerm_resource_group.ado.name
}

# Azure DevOps resolves a new service principal from Entra only once it has replicated.
resource "time_sleep" "argocd_propagation" {
  depends_on      = [azurerm_user_assigned_identity.argocd]
  create_duration = "60s"
}

resource "azuredevops_service_principal_entitlement" "argocd" {
  origin_id            = azurerm_user_assigned_identity.argocd.principal_id
  origin               = "aad"
  account_license_type = "express" # Basic: reading repos needs it
  depends_on           = [time_sleep.argocd_propagation]
}

data "azuredevops_group" "readers" {
  project_id = data.azuredevops_project.this.id
  name       = "Readers"
}

resource "azuredevops_group_membership" "argocd_readers" {
  group   = data.azuredevops_group.readers.descriptor
  members = [azuredevops_service_principal_entitlement.argocd.descriptor]
  mode    = "add"
}
