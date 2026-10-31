# Test infrastructure only: an AKS cluster to try the kubelet credential provider on.
# In production the cluster already exists and only the Kubernetes console's
# configure step runs against it. The OIDC issuer is the one thing the provider needs.

resource "azurerm_resource_group" "cluster" {
  name     = "rg-${var.name}"
  location = var.azure_location
  tags     = var.tags
}

resource "azurerm_kubernetes_cluster" "cluster" {
  name                      = var.name
  location                  = azurerm_resource_group.cluster.location
  resource_group_name       = azurerm_resource_group.cluster.name
  dns_prefix                = var.name
  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  tags                      = var.tags

  default_node_pool {
    name       = "system"
    node_count = var.node_count
    vm_size    = var.node_size
  }

  node_provisioning_profile {
    mode = "Manual"
  }

  identity {
    type = "SystemAssigned"
  }
}
