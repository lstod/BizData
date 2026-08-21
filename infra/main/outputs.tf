data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# What the scripts need.
#
# scripts/seed.py, scripts/bootstrap_aurora.py and scripts/warm.py all read the
# cluster and a secret out of the environment, so the usual first move after an
# apply is:
#
#   eval "$(terraform -chdir=infra/main output -raw shell_exports)"
#
# which is one line rather than four exports copied by hand, and cannot drift
# from what was actually deployed.
# ---------------------------------------------------------------------------

output "mcp_endpoint" {
  description = "The MCP endpoint to give Cowork's connector form."
  value       = "${aws_apigatewayv2_api.main.api_endpoint}/mcp"
}

output "api_base_url" {
  description = "Root of the HTTP API. The OAuth well-known documents hang off this, which is why the stage is $default."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "cluster_arn" {
  description = "Aurora cluster ARN, for the Data API."
  value       = aws_rds_cluster.main.arn
}

output "readonly_secret_arn" {
  description = "The SELECT-only credential the deployed server uses."
  value       = aws_secretsmanager_secret.mcp_readonly.arn
}

output "master_secret_arn" {
  description = "RDS-managed master credential. Used by the seed and bootstrap scripts from the laptop, and by nothing that is deployed."
  value       = aws_rds_cluster.main.master_user_secret[0].secret_arn
}

output "runs_bucket" {
  description = "Archive of record for step 9's publish_pack."
  value       = aws_s3_bucket.runs.id
}

output "log_group" {
  description = "Where the step-3 tool-call log lines land."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "api_log_group" {
  description = "HTTP API access logs, including the Mcp-Protocol-Version header."
  value       = aws_cloudwatch_log_group.api.name
}

output "shell_exports" {
  description = "Environment for the scripts. Use with: eval \"$(terraform -chdir=infra/main output -raw shell_exports)\""
  value = join("\n", [
    "export BIZDATA_DB_BACKEND=aws",
    "export BIZDATA_CLUSTER_ARN=${aws_rds_cluster.main.arn}",
    "export BIZDATA_SECRET_ARN=${aws_secretsmanager_secret.mcp_readonly.arn}",
    "export BIZDATA_MASTER_SECRET_ARN=${aws_rds_cluster.main.master_user_secret[0].secret_arn}",
    "export BIZDATA_DATABASE=${var.db_name}",
    "export BIZDATA_MCP_ENDPOINT=${aws_apigatewayv2_api.main.api_endpoint}/mcp",
  ])
}

# ---------------------------------------------------------------------------
# Cognito, empty until enable_auth is true.
# ---------------------------------------------------------------------------

output "cognito_issuer" {
  description = "Cognito's OIDC issuer. Its discovery document is at <issuer>/.well-known/openid-configuration."
  value       = var.enable_auth ? local.cognito_issuer : null
}

output "cognito_hosted_ui" {
  description = "Hosted UI domain, which serves /oauth2/authorize and /oauth2/token."
  value       = var.enable_auth ? local.cognito_domain : null
}

output "cognito_client_id" {
  description = "Paste into Cowork's \"OAuth Client ID (optional)\"."
  value       = var.enable_auth ? aws_cognito_user_pool_client.cowork[0].id : null
}

output "cognito_client_secret" {
  description = "Paste into Cowork's \"OAuth Client Secret (optional)\"."
  value       = var.enable_auth ? aws_cognito_user_pool_client.cowork[0].client_secret : null
  sensitive   = true
}

output "demo_user_email" {
  value       = var.enable_auth ? var.demo_user_email : null
  description = "The account to sign in with when the connector opens the hosted UI."
}

output "demo_user_password" {
  value       = var.enable_auth ? random_password.demo_user[0].result : null
  description = "Password for the demo user."
  sensitive   = true
}
