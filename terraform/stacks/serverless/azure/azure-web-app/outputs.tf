output "resource_group" {
  value = module.base.resource_group_name
}

output "workload_identity_client_id" {
  value = module.base.identity.client_id
}

output "app_name" {
  value = local.app.name
}

output "app_url" {
  value = "https://${local.app.default_hostname}"
}

output "os" {
  value = var.os
}

output "runtime" {
  value = var.runtime
}
