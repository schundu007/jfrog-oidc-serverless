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

output "argocd_repo_server_client_id" {
  description = "Argo CD repo-server's managed identity: give it Reader on the Azure DevOps project to read the GitOps repo."
  value       = var.argocd_enabled ? azurerm_user_assigned_identity.argocd_repo_server[0].client_id : null
}

output "argocd_repo_server_principal_id" {
  value = var.argocd_enabled ? azurerm_user_assigned_identity.argocd_repo_server[0].principal_id : null
}

output "argocd_ui_command" {
  value = var.argocd_enabled ? "kubectl -n argocd port-forward svc/argocd-server 8080:80" : null
}
