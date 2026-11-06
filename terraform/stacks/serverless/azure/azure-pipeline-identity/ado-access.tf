# The setup identity runs the ADO-side pipelines (Azure - Register pipelines, Pipelines' JFrog trust): it
# registers pipelines and writes the JFrog variable group, so it is a Project Administrator of this ADO
# project only. That is ADO access, not Azure: it still cannot assign Azure roles.
resource "azuredevops_service_principal_entitlement" "setup" {
  origin               = "aad"
  origin_id            = azurerm_user_assigned_identity.setup.principal_id
  account_license_type = "express"
}

data "azuredevops_group" "project_admins" {
  project_id = data.azuredevops_project.this.id
  name       = "Project Administrators"
}

resource "azuredevops_group_membership" "setup_admin" {
  group   = data.azuredevops_group.project_admins.descriptor
  members = [azuredevops_service_principal_entitlement.setup.descriptor]
  mode    = "add"
}
