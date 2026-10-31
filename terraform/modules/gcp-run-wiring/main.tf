# JFrog OIDC settings on an existing Cloud Run job or service, by Terraform and nothing else.
#
# Only the JFrog variables (and, for a dedicated identity, the service account) are ours. The
# live resource is read at plan time and sent back with just those changed, so its image, other
# variables, secrets and scaling stay whatever the owner deployed:
#   service: PATCH ?updateMask=template.containers,template.serviceAccount
#   job:     PATCH of the job's input fields (jobs.patch has no updateMask), as gcloud run jobs update does
# Destroy sends the live resource back without the JFrog variables.

data "google_client_config" "this" {}

locals {
  collection = var.kind == "job" ? "jobs" : "services"
  path       = "/v2/projects/${var.project}/locations/${var.region}/${local.collection}/${var.name}"
}

data "http" "live" {
  url             = "https://run.googleapis.com${local.path}"
  request_headers = { Authorization = "Bearer ${data.google_client_config.this.access_token}" }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "Cannot read Cloud Run ${local.collection}/${var.name} in ${var.project}/${var.region} (HTTP ${self.status_code})."
    }
  }
}

locals {
  live = jsondecode(data.http.live.response_body)
  # Input fields only: output-only ones are dropped, and so are etag (it changes on every write)
  # and the execution tokens (sending one starts an execution).
  job_inputs = ["name", "labels", "annotations", "client", "clientVersion", "launchStage", "binaryAuthorization", "functionalType"]
  owned      = distinct(concat(var.managed_keys, keys(var.env)))
  revision   = var.kind == "job" ? local.live.template.template : local.live.template

  containers = { for wired in [true, false] : tostring(wired) => [
    for i, c in local.revision.containers : merge(
      { for k, v in c : k => v if k != "buildInfo" },
      i != 0 ? {} : {
        env = concat(
          [for e in try(c.env, []) : e if !contains(local.owned, e.name)],
          wired ? [for k in sort(keys(var.env)) : { name = k, value = var.env[k] }] : [],
        )
      },
    )
  ] }

  revisions = { for wired in [true, false] : tostring(wired) => merge(
    local.revision,
    { containers = local.containers[tostring(wired)] },
    wired && var.service_account != null ? { serviceAccount = var.service_account } : {},
  ) }

  bodies = { for wired in ["true", "false"] : wired => (
    var.kind == "job"
    ? jsonencode(merge(
      { for k, v in local.live : k => v if contains(local.job_inputs, k) },
      { template = merge(local.live.template, { template = local.revisions[wired] }) },
    ))
    : jsonencode({ template = local.revisions[wired] })
  ) }
}

resource "restapi_object" "wiring" {
  path         = local.path
  create_path  = local.path
  read_path    = local.path
  update_path  = local.path
  destroy_path = local.path
  object_id    = var.name
  query_string = var.kind == "service" ? "updateMask=template.containers,template.serviceAccount" : ""

  create_method  = "PATCH"
  update_method  = "PATCH"
  destroy_method = "PATCH"

  data         = local.bodies["true"]
  destroy_data = local.bodies["false"]

  # The owner keeps deploying the resource: only a change in what we set is ours to apply.
  ignore_all_server_changes = true
}
