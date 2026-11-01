output "cluster_name" {
  value = azurerm_kubernetes_cluster.cluster.name
}

output "resource_group" {
  value = azurerm_resource_group.cluster.name
}

output "oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.cluster.oidc_issuer_url
}

output "kubeconfig_command" {
  value = "az aks get-credentials -g ${azurerm_resource_group.cluster.name} -n ${azurerm_kubernetes_cluster.cluster.name}"
}

output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.cluster.id
}
