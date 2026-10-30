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
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

# Admin-scoped token for the JFrog provider. In CI, obtain it the same way the
# workloads do (GitHub/ADO OIDC -> JFrog) rather than storing it: export
# JFROG_ACCESS_TOKEN from `jf eot` before `terraform apply`.
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

provider "azuread" {}

provider "azapi" {}
