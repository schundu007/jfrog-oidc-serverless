# Every workload identity that may talk to JFrog gets its own mapping and a
# read-only group. Runtime mappings are group-only (no username); pull mappings
# carry a fixed username because the platform's registry config needs one, so
# give those JFrog users no permissions beyond read.
#
# TTL guidance (see README for the math):
#   pull-path (rotator) mappings: 4h, rotated hourly -> survives 2 missed runs.
#   runtime mappings:          1h; the client refreshes 5 min before expiry.

module "jfrog_oidc" {
  source = "../../modules/jfrog-oidc"

  gcp = {
    provider_name = "gcp-cloudrun"
    audience      = "jfrog-gcp"
  }

  azure = {
    provider_name   = "azure-entra"
    tenant_id       = module.azure.tenant_id
    audience_app_id = module.azure.audience_app_id
  }

  workloads = {
    # --- pull path (rotators) -------------------------------------------------------
    "gcp-ar-upstream" = {
      cloud       = "gcp"
      subject     = module.gcp.rotator_unique_id
      username    = "svc-gcp-ar-upstream"
      groups      = [var.jfrog_reader_group]
      expires_in  = 14400
      description = "Artifact Registry remote repos upstream credential (rotated)"
    }
    "azure-registry-pull" = {
      cloud       = "azure"
      subject     = module.azure.rotator_principal_id
      username    = local.azure_registry_username
      groups      = [var.jfrog_reader_group]
      expires_in  = 14400
      description = "Container Apps / Functions registry credential (rotated)"
    }

    # --- runtime (in-process exchange) ----------------------------------------------
    "gcp-fn-orders" = {
      cloud   = "gcp"
      subject = google_service_account.fn_orders.unique_id
      groups  = [var.jfrog_reader_group]
    }
    "azure-aca-orders" = {
      cloud   = "azure"
      subject = azurerm_user_assigned_identity.aca_orders.principal_id
      groups  = [var.jfrog_reader_group]
    }
    "azure-func-orders" = {
      cloud   = "azure"
      subject = azurerm_user_assigned_identity.func_orders.principal_id
      groups  = [var.jfrog_reader_group]
    }
  }
}
