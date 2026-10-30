# GCP image/package pull path.
#
# Artifact Registry remote repositories proxy JFrog. Their upstream credential is
# a Secret Manager version holding a short-lived JFrog token. A Cloud Run job,
# running as its own service account, exchanges that account's Google ID token
# with JFrog every hour, adds a new secret version, repoints the repos at it and
# disables old versions. Cloud Run, Cloud Run functions builds and jobs pull from
# Artifact Registry with Google IAM; no JFrog credential ever reaches them.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  ar_service_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"
  ar_hosts = {
    DOCKER = "docker"
    NPM    = "npm"
    PYTHON = "python"
    MAVEN  = "maven"
  }
  reader_bindings = {
    for pair in setproduct(keys(var.remote_repositories), var.reader_members) :
    "${pair[0]}|${pair[1]}" => { repository = pair[0], member = pair[1] }
  }
}

resource "google_project_service" "apis" {
  for_each = var.enable_apis ? toset([
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ]) : toset([])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --- identities -------------------------------------------------------------

resource "google_service_account" "rotator" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-rotator"
  display_name = "JFrog OIDC upstream token rotator"
  description  = "Exchanges its Google ID token with JFrog and refreshes Artifact Registry upstream credentials."
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-rotator-sched"
  display_name = "Triggers the JFrog upstream token rotator"
}

# --- upstream credential secret ---------------------------------------------

resource "google_secret_manager_secret" "upstream" {
  project   = var.project_id
  secret_id = "${var.name_prefix}-upstream-token"

  replication {
    auto {}
  }

  labels = {
    managed-by = "terraform"
    rotated-by = "jfrog-oidc-rotator"
  }

  depends_on = [google_project_service.apis]
}

# Artifact Registry needs a secret version at create time. This placeholder is
# replaced by the first rotator run (see README "First run").
resource "google_secret_manager_secret_version" "bootstrap" {
  secret      = google_secret_manager_secret.upstream.id
  secret_data = "bootstrap-run-the-rotator"

  lifecycle {
    ignore_changes = [secret_data, enabled]
  }
}

resource "google_secret_manager_secret_iam_member" "ar_reads_upstream" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.upstream.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.ar_service_agent
}

resource "google_secret_manager_secret_iam_member" "rotator_manages_versions" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.upstream.secret_id
  role      = "roles/secretmanager.secretVersionManager"
  member    = "serviceAccount:${google_service_account.rotator.email}"
}

# --- remote repositories ----------------------------------------------------

resource "google_artifact_registry_repository" "remote" {
  for_each = var.remote_repositories

  project       = var.project_id
  location      = var.region
  repository_id = each.key
  format        = each.value.format
  mode          = "REMOTE_REPOSITORY"
  description   = coalesce(each.value.description, "JFrog proxy (${lower(each.value.format)}) with rotated short-lived credentials")

  remote_repository_config {
    description = "JFrog ${each.value.upstream_uri}"
    # The bootstrap placeholder can't authenticate yet; the first rotator run
    # replaces it.
    disable_upstream_validation = true

    dynamic "docker_repository" {
      for_each = each.value.format == "DOCKER" ? [1] : []
      content {
        custom_repository {
          uri = each.value.upstream_uri
        }
      }
    }

    dynamic "npm_repository" {
      for_each = each.value.format == "NPM" ? [1] : []
      content {
        custom_repository {
          uri = each.value.upstream_uri
        }
      }
    }

    dynamic "python_repository" {
      for_each = each.value.format == "PYTHON" ? [1] : []
      content {
        custom_repository {
          uri = each.value.upstream_uri
        }
      }
    }

    dynamic "maven_repository" {
      for_each = each.value.format == "MAVEN" ? [1] : []
      content {
        custom_repository {
          uri = each.value.upstream_uri
        }
      }
    }

    upstream_credentials {
      username_password_credentials {
        username                = var.registry_username
        password_secret_version = google_secret_manager_secret_version.bootstrap.name
      }
    }
  }

  labels = {
    managed-by = "terraform"
    upstream   = "jfrog"
  }

  # The rotator owns the credential pointer after the first run.
  lifecycle {
    ignore_changes = [remote_repository_config[0].upstream_credentials]
  }

  depends_on = [google_secret_manager_secret_iam_member.ar_reads_upstream]
}

# Least privilege for repointing credentials: get + update on these repos only.
resource "google_project_iam_custom_role" "rotator" {
  project     = var.project_id
  role_id     = "${replace(var.name_prefix, "-", "_")}_upstream_rotator"
  title       = "JFrog upstream credential rotator"
  description = "Update Artifact Registry remote repository upstream credentials."
  permissions = [
    "artifactregistry.repositories.get",
    "artifactregistry.repositories.update",
  ]
}

resource "google_artifact_registry_repository_iam_member" "rotator" {
  for_each = google_artifact_registry_repository.remote

  project    = var.project_id
  location   = each.value.location
  repository = each.value.name
  role       = google_project_iam_custom_role.rotator.name
  member     = "serviceAccount:${google_service_account.rotator.email}"
}

resource "google_artifact_registry_repository_iam_member" "readers" {
  for_each = local.reader_bindings

  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.remote[each.value.repository].name
  role       = "roles/artifactregistry.reader"
  member     = each.value.member
}

# --- rotator job + schedule ---------------------------------------------------

resource "google_cloud_run_v2_job" "rotator" {
  project             = var.project_id
  name                = "${var.name_prefix}-rotator"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = google_service_account.rotator.email
      max_retries     = 1
      timeout         = "300s"

      containers {
        image = var.rotator_image

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "ROTATOR_TARGET"
          value = "gcp"
        }
        env {
          name  = "JFROG_URL"
          value = var.jfrog_url
        }
        env {
          name  = "JFROG_OIDC_PROVIDER"
          value = var.jfrog_provider_name
        }
        env {
          name  = "JFROG_OIDC_AUDIENCE"
          value = var.jfrog_audience
        }
        env {
          name  = "GCP_SECRET"
          value = google_secret_manager_secret.upstream.id
        }
        env {
          name  = "AR_REPOSITORIES"
          value = join(",", [for r in google_artifact_registry_repository.remote : r.id])
        }
        env {
          name  = "KEEP_VERSIONS"
          value = tostring(var.keep_versions)
        }
        env {
          name  = "MIN_TTL_SECONDS"
          value = tostring(var.min_ttl_seconds)
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.rotator_manages_versions,
    google_artifact_registry_repository_iam_member.rotator,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_rotator" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.rotator.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "rotator" {
  project          = var.project_id
  region           = var.region
  name             = "${var.name_prefix}-rotator"
  description      = "Rotate the JFrog upstream token used by Artifact Registry remote repos"
  schedule         = var.rotation_schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "320s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.rotator.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_rotator]
}

# --- alerting: page before the token can expire -------------------------------

resource "google_logging_metric" "rotation_succeeded" {
  project = var.project_id
  name    = "${replace(var.name_prefix, "-", "_")}_rotation_succeeded"
  filter  = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${google_cloud_run_v2_job.rotator.name}\" AND jsonPayload.event=\"rotation_succeeded\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "rotation_stale" {
  count = length(var.notification_channels) > 0 ? 1 : 0

  project      = var.project_id
  display_name = "JFrog upstream token not rotated (${var.name_prefix})"
  combiner     = "OR"

  conditions {
    display_name = "No successful rotation for ${var.alert_after_seconds}s"
    condition_absent {
      filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.rotation_succeeded.name}\" AND resource.type=\"cloud_run_job\""
      duration = "${var.alert_after_seconds}s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.notification_channels

  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      The JFrog upstream token for Artifact Registry remote repos has not rotated.
      Pulls of anything not already cached will start failing when the current
      token expires. Check executions of Cloud Run job `${google_cloud_run_v2_job.rotator.name}`
      and run it manually: `gcloud run jobs execute ${google_cloud_run_v2_job.rotator.name} --region ${var.region} --wait`.
    EOT
  }
}
