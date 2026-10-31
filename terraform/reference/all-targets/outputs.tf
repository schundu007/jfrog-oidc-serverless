output "first_run" {
  description = "Run both rotators once after apply so the platforms stop holding the bootstrap placeholder."
  value = {
    gcp   = module.gcp.first_run_command
    azure = module.azure.first_run_command
  }
}

output "gcp_repository_urls" {
  value = module.gcp.repository_urls
}

output "azure_audience_uri" {
  value = module.azure.audience_uri
}
