terraform {
  required_version = ">= 1.6"
  required_providers {
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
  }
}
