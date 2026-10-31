terraform {
  required_version = ">= 1.17"
  required_providers {
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
    azuredevops = {
      source  = "microsoft/azuredevops"
      version = ">= 1.16"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.23"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = ">= 2.47"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id     = var.azure_subscription_id
  storage_use_azuread = true
}

provider "azuread" {
  tenant_id = var.azure_tenant_id
}

# Admin token from JFROG_ACCESS_TOKEN for this bootstrap apply; afterwards the
# setup pipeline gets admin through OIDC (mapping ado-setup) instead.
provider "platform" {
  url = var.jfrog_url
}

# The pipelines' settings live in an ADO variable group written from this stack's outputs,
# so nothing about the target is kept in the repo. Signs in as the az CLI's account.
provider "azuredevops" {
  org_service_url = "https://dev.azure.com/${var.ado_org}"
  use_cli         = true
}
