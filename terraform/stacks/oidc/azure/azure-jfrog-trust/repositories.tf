# This cloud's own repositories and the group that reads them. Build pipelines (ado-build) and
# runtime workloads (onboarding mappings) get var.jfrog_reader_group; nothing is shared with
# another cloud, so every key starts with "azure-". pipelines/vars/azure-<env>-jfrog.yml names
# the same keys (setup_console/tests check they match).

locals {
  remotes = {
    docker = "azure-docker-remote"
    pypi   = "azure-pypi-remote"
    maven  = "azure-maven-remote"
    nuget  = "azure-nuget-remote"
  }
  generic_local = "azure-generic-local"
}

resource "artifactory_remote_docker_repository" "docker" {
  key                           = local.remotes.docker
  description                   = "Docker Hub, for this cloud's image builds"
  url                           = "https://registry-1.docker.io/"
  enable_token_authentication   = true
  block_pushing_schema1         = true
  list_remote_folder_items      = false
  external_dependencies_enabled = false
  priority_resolution           = false
}

resource "artifactory_remote_pypi_repository" "pypi" {
  key         = local.remotes.pypi
  description = "PyPI, for this cloud's Python builds"
  url         = "https://files.pythonhosted.org"
}

resource "artifactory_remote_maven_repository" "maven" {
  key         = local.remotes.maven
  description = "Maven Central, for this cloud's Java builds"
  url         = "https://repo1.maven.org/maven2/"
}

resource "artifactory_remote_nuget_repository" "nuget" {
  key         = local.remotes.nuget
  description = "nuget.org, for this cloud's .NET builds"
  url         = "https://www.nuget.org/"
  v3_feed_url = "https://api.nuget.org/v3/index.json"
}

# What workloads read in their self-test (JFROG_HEALTHCHECK_PATH = azure-generic-local/hello.txt).
resource "artifactory_local_generic_repository" "generic" {
  key         = local.generic_local
  description = "Files this cloud's workloads read to prove their JFrog access"
}

resource "artifactory_artifact" "hello" {
  repository     = artifactory_local_generic_repository.generic.key
  path           = "/hello.txt"
  content_base64 = base64encode("hello from JFrog\n")
}

resource "platform_group" "readers" {
  name        = var.jfrog_reader_group
  description = "Azure pipelines and workloads: read this cloud's repositories, cache on its remotes"
}

resource "platform_permission" "read" {
  name = "${var.jfrog_reader_group}-read"
  artifact = {
    actions = {
      groups = [{ name = platform_group.readers.name, permissions = ["READ"] }]
    }
    targets = [{ name = artifactory_local_generic_repository.generic.key, include_patterns = ["**"] }]
  }
}

# A remote fetches what it hasn't cached only for callers that may deploy to it (WRITE = cache).
resource "platform_permission" "cache" {
  name = "${var.jfrog_reader_group}-remote-cache"
  artifact = {
    actions = {
      groups = [{ name = platform_group.readers.name, permissions = ["READ", "WRITE"] }]
    }
    targets = [
      { name = artifactory_remote_docker_repository.docker.key, include_patterns = ["**"] },
      { name = artifactory_remote_pypi_repository.pypi.key, include_patterns = ["**"] },
      { name = artifactory_remote_maven_repository.maven.key, include_patterns = ["**"] },
      { name = artifactory_remote_nuget_repository.nuget.key, include_patterns = ["**"] },
    ]
  }
}
