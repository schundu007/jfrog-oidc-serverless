terraform {
  required_version = ">= 1.17"
  required_providers {
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
    artifactory = {
      source  = "jfrog/artifactory"
      version = ">= 12.0"
    }
    azuredevops = {
      source  = "microsoft/azuredevops"
      version = ">= 1.16"
    }
  }
}

# Admin token from JFROG_ACCESS_TOKEN (the operator's, for this local apply).
provider "platform" {
  url = var.jfrog_url
}

# Repositories and the test file, with the same token.
provider "artifactory" {
  url = var.jfrog_url
}

# Reads the foundation's service connections and writes this root's own variable group.
provider "azuredevops" {
  org_service_url = "https://dev.azure.com/${var.ado_org}"
  use_cli         = true
}
