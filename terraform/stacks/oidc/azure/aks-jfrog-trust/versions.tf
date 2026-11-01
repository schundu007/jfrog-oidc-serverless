terraform {
  required_version = ">= 1.17"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.23"
    }
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.36"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

# Admin token from JFROG_ACCESS_TOKEN: in the stack-setup run, the setup service connection's OIDC
# token exchanged through the ado-setup mapping (azure-jfrog-trust). Never a stored token.
provider "platform" {
  url = var.jfrog_url
}

# The cluster's own credentials, read through the run's Azure login.
provider "kubernetes" {
  host                   = data.azurerm_kubernetes_cluster.cluster.kube_config[0].host
  client_certificate     = base64decode(data.azurerm_kubernetes_cluster.cluster.kube_config[0].client_certificate)
  client_key             = base64decode(data.azurerm_kubernetes_cluster.cluster.kube_config[0].client_key)
  cluster_ca_certificate = base64decode(data.azurerm_kubernetes_cluster.cluster.kube_config[0].cluster_ca_certificate)
}
