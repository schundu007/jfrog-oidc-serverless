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
  }
}

# Admin-scoped token comes from JFROG_ACCESS_TOKEN at apply time; it is never
# stored in this configuration.
provider "platform" {
  url = var.jfrog_url
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}
