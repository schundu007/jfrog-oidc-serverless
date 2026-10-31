terraform {
  required_version = ">= 1.7"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.13"
    }
    http = {
      source  = "hashicorp/http"
      version = ">= 3.4"
    }
    # Configured by the caller: uri = https://run.googleapis.com, bearer token from google_client_config.
    restapi = {
      source  = "Mastercard/restapi"
      version = ">= 2.0"
    }
  }
}
