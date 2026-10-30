output "acr_login_server" {
  value = azurerm_container_registry.platform.login_server
}

output "demo_identity_client_id" {
  value = azurerm_user_assigned_identity.demo.client_id
}

output "image_build_command" {
  description = "Build samples/cloud-run-job into the example's ACR (run from the repo root)."
  value       = "az acr build --registry ${azurerm_container_registry.platform.name} --image ${var.demo_image} --file samples/cloud-run-job/Dockerfile ."
}

output "runtime_demo_command" {
  value = var.deploy_demo ? "az containerapp job start --name ${azurerm_container_app_job.runtime_demo[0].name} --resource-group ${azurerm_resource_group.demo.name}" : null
}
