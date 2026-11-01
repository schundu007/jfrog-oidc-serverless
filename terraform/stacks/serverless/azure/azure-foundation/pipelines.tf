# Pipeline definitions this stack registers in ADO (the others were registered before it did).
# aks-jfrog-trust's stack-setup runs after aks-cluster: the cluster's JFrog OIDC and credential provider.

variable "ado_repository" {
  description = "ADO Git repository that holds the pipelines (pipelines/delivery.json). Empty: register nothing."
  type        = string
  default     = ""
}

data "azuredevops_git_repository" "pipelines" {
  count      = var.ado_repository == "" ? 0 : 1
  project_id = data.azuredevops_project.this.id
  name       = var.ado_repository
}

resource "azuredevops_build_definition" "aks_jfrog_trust" {
  count      = var.ado_repository == "" ? 0 : 1
  project_id = data.azuredevops_project.this.id
  name       = "AKS credential provider - JFrog OIDC"
  path       = "\\Kubernetes\\Azure"

  ci_trigger {
    use_yaml = true
  }

  repository {
    repo_type   = "TfsGit"
    repo_id     = data.azuredevops_git_repository.pipelines[0].id
    branch_name = data.azuredevops_git_repository.pipelines[0].default_branch
    yml_path    = "pipelines/oidc/azure/aks-jfrog-trust/stack-setup.yml"
  }
}
