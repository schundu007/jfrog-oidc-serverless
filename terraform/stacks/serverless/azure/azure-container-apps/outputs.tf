output "acr_login_server" {
  value = module.base.acr_login_server
}

output "workload_identity_client_id" {
  value = module.base.identity.client_id
}

output "resource_group" {
  value = module.base.resource_group_name
}

output "job_name" {
  value = var.deploy_job ? azurerm_container_app_job.workload[0].name : null
}

output "runtime_principal_id" {
  description = "Object ID of the workload's runtime identity (system- or user-assigned)."
  value       = local.runtime_principal_id
}

output "admin_role_grants" {
  description = "Azure roles the runtime identity needs; an admin grants them through azure-pipeline-identity (workloads.<name>.runtime_principal_id)."
  value       = [for r in module.base.runtime_roles : merge(r, { principal_id = local.runtime_principal_id })]
}
