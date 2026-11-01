# Argo CD hub for the KCP fleet (gitops/argocd/values.yaml): no admin password, no Dex, no stored
# credentials. Its repo-server signs in as its own managed identity through Azure Workload Identity
# (federated to the argocd-repo-server service account), so reading the Azure DevOps repo needs no secret.

data "azurerm_client_config" "current" {}

resource "azurerm_user_assigned_identity" "argocd_repo_server" {
  count               = var.argocd_enabled ? 1 : 0
  name                = "id-${var.name}-argocd-repo-server"
  location            = azurerm_resource_group.cluster.location
  resource_group_name = azurerm_resource_group.cluster.name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "argocd_repo_server" {
  count                     = var.argocd_enabled ? 1 : 0
  name                      = "argocd-repo-server"
  user_assigned_identity_id = azurerm_user_assigned_identity.argocd_repo_server[0].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.cluster.oidc_issuer_url
  subject                   = "system:serviceaccount:argocd:argocd-repo-server"
}

resource "helm_release" "argocd" {
  count            = var.argocd_enabled ? 1 : 0
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.argocd_chart_version
  timeout          = 900
  values           = [file("${path.module}/../../../../../gitops/argocd/values.yaml")]
  set = [
    { name = "repoServer.serviceAccount.annotations.azure\\.workload\\.identity/client-id", value = azurerm_user_assigned_identity.argocd_repo_server[0].client_id },
    { name = "repoServer.serviceAccount.annotations.azure\\.workload\\.identity/tenant-id", value = data.azurerm_client_config.current.tenant_id },
  ]
  depends_on = [azurerm_federated_identity_credential.argocd_repo_server]
}
