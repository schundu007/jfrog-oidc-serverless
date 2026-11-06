# Offline: a mock azurerm provider, no Azure calls. Each supported OS/runtime plans; Windows + Python/PHP and slots on Basic are refused.
mock_provider "azurerm" {}

variables {
  azure_subscription_id = "00000000-0000-0000-0000-000000000000"
}

run "linux_python" {
  command = plan
  variables {
    os      = "linux"
    runtime = "python"
  }
  assert {
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].python_version == "3.13"
    error_message = "linux python must run 3.13"
  }
  assert {
    condition     = length(azurerm_windows_web_app.workload) == 0
    error_message = "linux must not create a windows app"
  }
}

run "linux_java" {
  command = plan
  variables {
    os      = "linux"
    runtime = "java"
  }
  assert {
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].java_server == "JAVA"
    error_message = "linux java must run Java SE"
  }
}

run "linux_dotnet" {
  command = plan
  variables {
    os      = "linux"
    runtime = "dotnet"
  }
  assert {
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].dotnet_version == "10.0"
    error_message = "linux dotnet must run 10.0"
  }
}

run "windows_java" {
  command = plan
  variables {
    os      = "windows"
    runtime = "java"
  }
  assert {
    condition     = azurerm_windows_web_app.workload[0].site_config[0].application_stack[0].current_stack == "java"
    error_message = "windows java stack"
  }
  assert {
    condition     = length(azurerm_linux_web_app.workload) == 0
    error_message = "windows must not create a linux app"
  }
}

run "windows_dotnet" {
  command = plan
  variables {
    os      = "windows"
    runtime = "dotnet"
  }
  assert {
    condition     = azurerm_windows_web_app.workload[0].site_config[0].application_stack[0].dotnet_version == "v10.0"
    error_message = "windows dotnet must run v10.0"
  }
}

run "windows_python_refused" {
  command = plan
  variables {
    os      = "windows"
    runtime = "python"
  }
  expect_failures = [azurerm_service_plan.this]
}

run "tags_name_the_build" {
  command = plan
  variables {
    os      = "windows"
    runtime = "dotnet"
  }
  assert {
    condition     = azurerm_windows_web_app.workload[0].tags["jfrog-oidc-runtime"] == "dotnet" && azurerm_windows_web_app.workload[0].tags["jfrog-oidc-version"] == "10.0"
    error_message = "the web app must carry its runtime tags"
  }
}

run "linux_node" {
  command = plan
  variables {
    os      = "linux"
    runtime = "node"
  }
  assert {
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].node_version == "24-lts" && azurerm_linux_web_app.workload[0].site_config[0].app_command_line == "node server.js"
    error_message = "linux node must run 24-lts with node server.js"
  }
}

run "windows_node" {
  command = plan
  variables {
    os      = "windows"
    runtime = "node"
  }
  assert {
    condition     = azurerm_windows_web_app.workload[0].site_config[0].application_stack[0].current_stack == "node" && azurerm_windows_web_app.workload[0].site_config[0].application_stack[0].node_version == "~24"
    error_message = "windows node must run ~24"
  }
}

run "linux_php" {
  command = plan
  variables {
    os      = "linux"
    runtime = "php"
  }
  assert {
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].php_version == "8.4"
    error_message = "linux php must run 8.4"
  }
}

run "windows_php_refused" {
  command = plan
  variables {
    os      = "windows"
    runtime = "php"
  }
  expect_failures = [azurerm_service_plan.this]
}

run "slot_on_standard" {
  command = plan
  variables {
    os      = "linux"
    runtime = "node"
    sku     = "S1"
    slot    = "staging"
  }
  assert {
    condition     = azurerm_linux_web_app_slot.slot[0].name == "staging" && length(azurerm_linux_web_app_slot.slot[0].identity[0].identity_ids) == 1
    error_message = "the slot must exist with the workload identity"
  }
}

run "slot_on_basic_refused" {
  command = plan
  variables {
    sku  = "B1"
    slot = "staging"
  }
  expect_failures = [azurerm_service_plan.this]
}

run "no_slot_by_default" {
  command = plan
  assert {
    condition     = length(azurerm_linux_web_app_slot.slot) == 0 && length(azurerm_windows_web_app_slot.slot) == 0
    error_message = "no slot unless asked"
  }
}
