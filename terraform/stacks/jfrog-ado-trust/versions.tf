terraform {
  required_version = ">= 1.17"
  required_providers {
    platform = {
      source  = "jfrog/platform"
      version = ">= 2.2.1"
    }
  }
}

# Admin token from JFROG_ACCESS_TOKEN for this bootstrap apply; afterwards the
# setup pipeline gets admin through OIDC (mapping ado-setup) instead.
provider "platform" {
  url = var.jfrog_url
}
