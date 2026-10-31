data "google_project" "this" {
  project_id = var.gcp_project_id
}

locals {
  jfrog_host = trimprefix(var.jfrog_url, "https://")
  # Registry host for every image pull: the Docker repo's dedicated host,
  # e.g. acme-docker-virtual.jfrog.io. Image refs are <docker_host>/<image>:<tag>.
  docker_host = coalesce(
    var.jfrog_docker_host,
    "${split(".", local.jfrog_host)[0]}-${var.jfrog_docker_repo}.${join(".", slice(split(".", local.jfrog_host), 1, length(split(".", local.jfrog_host))))}",
  )
  # Cloud Run functions builds run as this account by default; adjust if you set
  # a custom build service account.
  gcp_build_sa = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

# Pull path: AR remote repos in front of JFrog + hourly rotator.
module "gcp" {
  source = "../../modules/gcp-pull-path"

  project_id          = var.gcp_project_id
  region              = var.gcp_region
  jfrog_url           = var.jfrog_url
  jfrog_provider_name = module.jfrog_oidc.gcp_provider_name
  jfrog_audience      = module.jfrog_oidc.gcp_audience
  registry_username   = "svc-gcp-ar-upstream"
  rotator_image       = var.gcp_rotator_image

  remote_repositories = {
    # Images resolve as <region>-docker.pkg.dev/<project>/jfrog-docker/<image>:<tag>.
    "jfrog-docker" = { format = "DOCKER", upstream_uri = "https://${local.docker_host}" }
    "jfrog-pypi"   = { format = "PYTHON", upstream_uri = "${var.jfrog_url}/artifactory/api/pypi/pypi-virtual" }
    "jfrog-npm"    = { format = "NPM", upstream_uri = "${var.jfrog_url}/artifactory/api/npm/npm-virtual" }
  }

  reader_members = [
    local.gcp_build_sa,
    "serviceAccount:${google_service_account.fn_orders.email}",
  ]

  notification_channels = var.gcp_notification_channels
}

# Runtime identity for a Cloud Run function. Its Google ID token is exchanged
# in-process (client/ library) for a 1h JFrog token.
resource "google_service_account" "fn_orders" {
  project      = var.gcp_project_id
  account_id   = "fn-orders"
  display_name = "Cloud Run function: orders"
}

resource "google_cloudfunctions2_function" "orders" {
  project  = var.gcp_project_id
  name     = "orders"
  location = var.gcp_region

  build_config {
    runtime     = "python312"
    entry_point = "handler"
    source {
      storage_source {
        bucket = var.gcp_function_source.bucket
        object = var.gcp_function_source.object
      }
    }
    # requirements.txt starts with:
    #   --index-url https://<region>-python.pkg.dev/<project>/jfrog-pypi/simple/
    # The build authenticates to Artifact Registry itself; JFrog sees only the
    # rotated upstream token.
  }

  service_config {
    service_account_email = google_service_account.fn_orders.email
    available_memory      = "512M"
    timeout_seconds       = 60
    environment_variables = {
      JFROG_URL           = var.jfrog_url
      JFROG_OIDC_PROVIDER = module.jfrog_oidc.gcp_provider_name
      JFROG_OIDC_AUDIENCE = module.jfrog_oidc.gcp_audience
      JFROG_OIDC_SOURCE   = "gcp"
    }
  }
}
