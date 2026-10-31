terraform {
  required_version = ">= 1.17"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.23"
    }
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
  # The workload's storage has no access keys: the provider reads it with Entra ID.
  storage_use_azuread = true
}

provider "azapi" {}
