output "jfrog_provider_name" {
  value = platform_oidc_configuration.ado.name
}

output "repositories" {
  description = "This cloud's repositories: docker, pypi, maven, nuget remotes and the generic local."
  value       = merge(local.remotes, { generic = artifactory_local_generic_repository.generic.key })
}

output "healthcheck_path" {
  description = "What workloads read in their self-test (JFROG_HEALTHCHECK_PATH)."
  value       = "${artifactory_local_generic_repository.generic.key}${artifactory_artifact.hello.path}"
}
