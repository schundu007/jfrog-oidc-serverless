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
    # Which nodes get the kubelet credential provider (aks-jfrog-trust installs it on them).
    node_labels = var.node_labels
  }

  node_provisioning_profile {
    mode = "Manual"
  }

  identity {
    type = "SystemAssigned"
  }

  # Azure Monitor (Container Insights): pod logs, Kubernetes events and the
  # nodes' syslog (kubelet runs the credential provider) kept in Log Analytics.
  oms_agent {
    log_analytics_workspace_id      = azurerm_log_analytics_workspace.cluster.id
    msi_auth_for_monitoring_enabled = true
  }
}

resource "azurerm_log_analytics_workspace" "cluster" {
  name                = "log-${var.name}"
  location            = azurerm_resource_group.cluster.location
  resource_group_name = azurerm_resource_group.cluster.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azurerm_monitor_data_collection_rule" "cluster" {
  name                = "MSCI-${var.azure_location}-${var.name}"
  location            = azurerm_resource_group.cluster.location
  resource_group_name = azurerm_resource_group.cluster.name
  kind                = "Linux"
  tags                = var.tags

  destinations {
    log_analytics {
      name                  = "ciworkspace"
      workspace_resource_id = azurerm_log_analytics_workspace.cluster.id
    }
  }

  data_flow {
    streams      = ["Microsoft-ContainerInsights-Group-Default", "Microsoft-Syslog"]
    destinations = ["ciworkspace"]
  }

  data_sources {
    extension {
      name           = "ContainerInsightsExtension"
      extension_name = "ContainerInsights"
      streams        = ["Microsoft-ContainerInsights-Group-Default"]
      extension_json = jsonencode({
        dataCollectionSettings = {
          interval               = "1m"
          namespaceFilteringMode = "Off"
          enableContainerLogV2   = true
        }
      })
    }

    syslog {
      name           = "nodesyslog"
      streams        = ["Microsoft-Syslog"]
      facility_names = ["*"]
      log_levels     = ["Debug", "Info", "Notice", "Warning", "Error", "Critical", "Alert", "Emergency"]
    }
  }
}

resource "azurerm_monitor_data_collection_rule_association" "cluster" {
  name                    = "ContainerInsightsExtension"
  target_resource_id      = azurerm_kubernetes_cluster.cluster.id
  data_collection_rule_id = azurerm_monitor_data_collection_rule.cluster.id
}
