jfrog_url             = "https://trial9hnw59.jfrog.io"
jfrog_reader_group    = "serverless-readers"
azure_subscription_id = "0e56d318-75b3-43ef-a41b-6a1a3c95b458"
azure_location        = "westus2"
workload              = "artifact-reader"
deployer_principal_id = "88c8ad8e-e5a6-42ee-bc66-d29fec03cefb"
artifact_path         = "demo-generic/hello.txt"
tags                  = { owner = "platform", managed-by = "terraform" }
deploy_job            = true
job_image             = "artifact-reader:27399e3c4909"
