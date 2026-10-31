terraform {
  required_version = ">= 1.17"
  required_providers {
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
