# Argo CD hub for the KCP fleet (gitops/argocd/values.yaml): no admin password, no Dex, no stored
# credentials. Its repo-server signs in as Argo CD's account (created by azure-foundation, ADO Basic and
# project Readers) through Azure Workload Identity: this stack federates that identity to the new
# cluster's OIDC issuer for the argocd-repo-server service account. Destroy removes only the federation.

data "azurerm_client_config" "current" {}

data "azurerm_user_assigned_identity" "argocd" {
  count               = var.argocd_enabled ? 1 : 0
  name                = var.argocd_identity.name
  resource_group_name = var.argocd_identity.resource_group
}

resource "azurerm_federated_identity_credential" "argocd_repo_server" {
  count                     = var.argocd_enabled ? 1 : 0
  name                      = "argocd-repo-server-${var.name}"
  user_assigned_identity_id = data.azurerm_user_assigned_identity.argocd[0].id
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
    { name = "repoServer.serviceAccount.annotations.azure\\.workload\\.identity/client-id", value = data.azurerm_user_assigned_identity.argocd[0].client_id },
    { name = "repoServer.serviceAccount.annotations.azure\\.workload\\.identity/tenant-id", value = data.azurerm_client_config.current.tenant_id },
  ]
  depends_on = [azurerm_federated_identity_credential.argocd_repo_server]
}
