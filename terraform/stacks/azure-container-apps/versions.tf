terraform {
  required_version = ">= 1.17"
  required_providers {
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.23"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = ">= 2.47"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

# Admin-scoped token comes from JFROG_ACCESS_TOKEN at apply time; it is never
# stored in this configuration.
provider "platform" {
  url = var.jfrog_url
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

# Tenant comes from the az CLI login; use an isolated AZURE_CONFIG_DIR when
# more than one tenant is signed in.
provider "azuread" {}
