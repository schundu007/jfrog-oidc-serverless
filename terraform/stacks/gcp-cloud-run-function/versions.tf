terraform {
  required_version = ">= 1.17"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.13"
    }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}
