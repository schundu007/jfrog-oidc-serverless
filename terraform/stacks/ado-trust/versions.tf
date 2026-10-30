terraform {
  required_version = ">= 1.17"
  required_providers {
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
    google = {
      source  = "hashicorp/google"
      version = ">= 6.13"
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

# Admin token from JFROG_ACCESS_TOKEN for this one bootstrap apply; afterwards the
# setup pipeline gets admin through OIDC (mapping ado-setup) instead.
provider "platform" {
  url = var.jfrog_url
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

provider "azurerm" {
  features {}
  subscription_id     = var.azure_subscription_id
  storage_use_azuread = true
}

provider "azuread" {
  tenant_id = var.azure_tenant_id
}
