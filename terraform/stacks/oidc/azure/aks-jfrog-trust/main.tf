# The AKS cluster's JFrog OIDC: everything that lets its nodes pull from JFrog with no stored
# credential. Runs after aks-cluster, in its own stack-setup pipeline, so each new cluster (new
# issuer) is trusted again on every redeploy. aks-cluster itself never touches JFrog.
#   JFrog:   an OIDC provider for the cluster's issuer and the mapping of its service account.
#   Cluster: the service account, and an Argo CD Application that installs the
#            jfrog-credential-provider chart on the labelled nodes and keeps it in sync.

locals {
  host         = trimprefix(var.jfrog_url, "https://")
  issuer       = data.azurerm_kubernetes_cluster.cluster.oidc_issuer_url
  provider     = "${var.cluster_name}-oidc"
  audience     = "api://AzureADTokenExchange"
  match_images = length(var.match_images) > 0 ? var.match_images : [local.host]
}

data "azurerm_kubernetes_cluster" "cluster" {
  name                = var.cluster_name
  resource_group_name = var.cluster_resource_group
}

# -- JFrog ----------------------------------------------------------------------------------------

resource "platform_oidc_configuration" "cluster" {
  name          = local.provider
  description   = "AKS ${var.cluster_name} workload identity (kubelet credential provider)"
  issuer_url    = local.issuer
  provider_type = "Azure"
  audience      = local.audience
}

resource "platform_oidc_identity_mapping" "nodes" {
  name          = "${var.cluster_name}-mapping"
  description   = "${var.namespace}/${var.service_account} on ${var.cluster_name} pulls as ${var.mapped_user}"
  provider_name = platform_oidc_configuration.cluster.name
  priority      = 1
  claims_json = jsonencode({
    iss = local.issuer
    sub = "system:serviceaccount:${var.namespace}:${var.service_account}"
  })
  token_spec = {
    username   = var.mapped_user
    scope      = var.token_scope
    audience   = var.token_audience
    expires_in = var.token_expires_in
  }
}

# -- Cluster --------------------------------------------------------------------------------------

resource "kubernetes_namespace_v1" "provider" {
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_service_account_v1" "provider" {
  metadata {
    name        = var.service_account
    namespace   = kubernetes_namespace_v1.provider.metadata[0].name
    annotations = { JFrogExchange = "true" }
  }
}

# Argo CD (installed by aks-cluster) installs the chart from charts.jfrog.io and keeps it in sync.
# Its values are this stack's inputs; nothing about the target is committed.
resource "kubernetes_manifest" "credential_provider" {
  manifest = {
    apiVersion = "argoproj.io/v1alpha1"
    kind       = "Application"
    metadata = {
      name       = "jfrog-credential-provider"
      namespace  = "argocd"
      finalizers = ["resources-finalizer.argocd.argoproj.io"]
    }
    spec = {
      project = "default"
      source = {
        repoURL        = "https://charts.jfrog.io"
        chart          = "jfrog-credential-provider"
        targetRevision = var.chart_version
        helm = {
          releaseName = "jfrog-cred-provider"
          valuesObject = {
            providerConfig = [{
              name                 = "jfrog-credentials-provider"
              artifactoryUrl       = local.host
              matchImages          = local.match_images
              defaultCacheDuration = var.cache_duration
              tokenAttributes = {
                enabled                     = true
                serviceAccountTokenAudience = local.audience
              }
              azure = {
                enabled                  = true
                azure_app_audience       = local.audience
                jfrog_oidc_provider_name = platform_oidc_configuration.cluster.name
              }
            }]
            rbac = { create = true }
            affinity = {
              nodeAffinity = {
                requiredDuringSchedulingIgnoredDuringExecution = {
                  nodeSelectorTerms = [{
                    matchExpressions = [{ key = var.node_label, operator = "In", values = ["true"] }]
                  }]
                }
              }
            }
          }
        }
      }
      destination = {
        server    = "https://kubernetes.default.svc"
        namespace = kubernetes_namespace_v1.provider.metadata[0].name
      }
      syncPolicy = {
        automated = { prune = true, selfHeal = true }
      }
    }
  }
  depends_on = [platform_oidc_identity_mapping.nodes, kubernetes_service_account_v1.provider]
}
