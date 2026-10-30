terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.26.0 added aws_iam_outbound_web_identity_federation.
      version = ">= 6.26"
    }
  }
}
