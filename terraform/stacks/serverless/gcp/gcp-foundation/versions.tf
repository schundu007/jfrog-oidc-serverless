terraform {
  required_version = ">= 1.17"
  required_providers {
    azuredevops = {
      source  = "microsoft/azuredevops"
      version = ">= 1.16"
    }
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
    google = {
      source  = "hashicorp/google"
      version = ">= 6.13"
    }
  }
}

# Admin token from JFROG_ACCESS_TOKEN for this bootstrap apply; afterwards the
# setup pipeline gets admin through OIDC (mapping ado-setup) instead.
provider "platform" {
  url = var.jfrog_url
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

# The pipelines' settings live in an ADO variable group written from this stack's outputs,
# so nothing about the target is kept in the repo. Signs in as the az CLI's account.
provider "azuredevops" {
  org_service_url = "https://dev.azure.com/${var.ado_org}"
  use_cli         = true
}
