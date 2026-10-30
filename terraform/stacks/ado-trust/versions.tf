terraform {
  required_version = ">= 1.7"
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
  subscription_id = var.azure_subscription_id
}
