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
    # For scripts/check_archive.py, which reads the bucket directly to assert the
    # things a presigned URL cannot show: versioning, the lifecycle rule, and that
    # the four objects a finalised run claims are really there.
    "export BIZDATA_RUNS_BUCKET=${aws_s3_bucket.runs.id}",
    "export BIZDATA_LOG_GROUP=${aws_cloudwatch_log_group.lambda.name}",
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

output "auth_exports" {
  description = <<-EOT
    Environment for scripts/check_auth.py, which proves the authentication chain without a
    browser. Use with: eval "$(terraform -chdir=infra/main output -raw auth_exports)"

    Sensitive because it carries the test client's secret. That client can do
    client_credentials only, so the worst it can mint is a token with bizdata/read against
    a database of synthetic rows — but it is a credential and is marked as one.
  EOT
  sensitive   = true
  value = var.enable_auth ? join("\n", [
    "export BIZDATA_OAUTH_ISSUER=${local.cognito_issuer}",
    "export BIZDATA_OAUTH_TOKEN_ENDPOINT=${local.cognito_domain}/oauth2/token",
    "export BIZDATA_OAUTH_AUTHORIZATION_ENDPOINT=${local.cognito_domain}/oauth2/authorize",
    "export BIZDATA_OAUTH_SCOPES=${join(",", aws_cognito_resource_server.bizdata[0].scope_identifiers)}",
    "export BIZDATA_TEST_CLIENT_ID=${aws_cognito_user_pool_client.machine[0].id}",
    "export BIZDATA_TEST_CLIENT_SECRET=${aws_cognito_user_pool_client.machine[0].client_secret}",
  ]) : ""
}

output "cowork_connector_settings" {
  description = "Everything Cowork's custom connector form asks for, in one place."
  sensitive   = true
  value = var.enable_auth ? join("\n", [
    "URL                  ${aws_apigatewayv2_api.main.api_endpoint}/mcp",
    "OAuth Client ID      ${aws_cognito_user_pool_client.cowork[0].id}",
    "OAuth Client Secret  ${aws_cognito_user_pool_client.cowork[0].client_secret}",
    "Sign in as           ${var.demo_user_email} / ${random_password.demo_user[0].result}",
  ]) : "enable_auth is false; the endpoint is open and the connector needs no client."
}
