output "oidc_provider" {
  value = platform_oidc_configuration.cluster.name
}

output "issuer" {
  value = local.issuer
}

output "argocd_application" {
  value = "argocd/jfrog-credential-provider"
}
