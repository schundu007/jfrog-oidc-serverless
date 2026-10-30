output "dedicated_role_arns" {
  description = "<workload> => ARN of the role created for it (the JFrog mapping's subject)."
  value       = { for k, r in aws_iam_role.dedicated : k => r.arn }
}

output "account_id" {
  description = "Account the roles live in."
  value       = data.aws_caller_identity.current.account_id
}

output "issuer" {
  description = "The account's outbound federation issuer, when this module enabled it."
  value       = one(aws_iam_outbound_web_identity_federation.this[*].issuer_identifier)
}
