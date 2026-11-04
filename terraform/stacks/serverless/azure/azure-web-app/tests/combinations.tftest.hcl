# Offline: a mock azurerm provider, no Azure calls. Each supported OS/runtime plans; Windows + Python is refused.
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
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].python_version == "3.12"
    error_message = "linux python must run 3.12"
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
    condition     = azurerm_linux_web_app.workload[0].site_config[0].application_stack[0].dotnet_version == "8.0"
    error_message = "linux dotnet must run 8.0"
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
    condition     = azurerm_windows_web_app.workload[0].site_config[0].application_stack[0].dotnet_version == "v8.0"
    error_message = "windows dotnet must run v8.0"
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
    condition     = azurerm_windows_web_app.workload[0].tags["jfrog-oidc-runtime"] == "dotnet" && azurerm_windows_web_app.workload[0].tags["jfrog-oidc-version"] == "8.0"
    error_message = "the web app must carry its runtime tags"
  }
}
