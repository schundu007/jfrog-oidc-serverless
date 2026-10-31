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
