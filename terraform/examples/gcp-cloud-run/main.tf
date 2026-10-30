# GCP only, end to end: a Cloud Run job reads JFrog artifacts with a short-lived
# OIDC token. Images come from Artifact Registry and are pulled by Cloud Run with
# its own identity; JFrog holds artifacts (files, packages), not images. No stored
# credentials anywhere.
#
#   apply 1 (deploy_demo = false): JFrog trust for Google tokens, the demo job's
#           mapping, the image repo, the service account.
#   then:   build samples/cloud-run-job into Artifact Registry.
#   apply 2 (deploy_demo = true): the Cloud Run job that exchanges its Google
#           identity for a 1 h JFrog token and downloads var.demo_artifact.

module "jfrog_oidc" {
  source = "../../modules/jfrog-oidc"

  gcp = {
    provider_name = "gcp-cloudrun"
    audience      = "jfrog-gcp"
  }

  workloads = {
    "gcp-run-demo" = {
      cloud       = "gcp"
      subject     = google_service_account.demo.unique_id
      groups      = [var.jfrog_reader_group]
      expires_in  = 3600
      description = "Cloud Run demo job runtime token"
    }
  }
}

# Images live in Artifact Registry; Cloud Run pulls them with its own identity.
resource "google_artifact_registry_repository" "platform" {
  project       = var.gcp_project_id
  location      = var.gcp_region
  repository_id = "platform"
  format        = "DOCKER"
  description   = "Workload images (not proxied through JFrog)"
}

resource "google_service_account" "demo" {
  project      = var.gcp_project_id
  account_id   = "jfrog-oidc-demo"
  display_name = "JFrog OIDC demo (Cloud Run)"
}

# Runtime artifact read: the job exchanges its Google ID token for a 1 h JFrog
# token (client/ library) and downloads var.demo_artifact.
resource "google_cloud_run_v2_job" "runtime_demo" {
  count               = var.deploy_demo ? 1 : 0
  project             = var.gcp_project_id
  name                = "jfrog-runtime-demo"
  location            = var.gcp_region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.demo.email
      max_retries     = 0
      containers {
        image = var.demo_image
        env {
          name  = "JFROG_URL"
          value = var.jfrog_url
        }
        env {
          name  = "JFROG_OIDC_PROVIDER"
          value = module.jfrog_oidc.gcp_provider_name
        }
        env {
          name  = "JFROG_OIDC_AUDIENCE"
          value = module.jfrog_oidc.gcp_audience
        }
        env {
          name  = "JFROG_OIDC_SOURCE"
          value = "gcp"
        }
        env {
          name  = "DEMO_ARTIFACT"
          value = var.demo_artifact
        }
      }
    }
  }
}
