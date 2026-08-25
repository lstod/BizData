variable "region" {
  description = "Region for all BizData resources."
  type        = string
  default     = "us-west-2"
}

variable "db_name" {
  description = "The database inside the Aurora cluster."
  type        = string
  default     = "bizdata"
}

variable "engine_version" {
  description = <<-EOT
    Aurora PostgreSQL version. 16.10 matches the Postgres 16.10 in docker-compose.yml,
    so local and deployed run the same engine rather than merely the same major.
    Auto-pause needs 16.3 or later; confirmed against this account with
    `aws rds describe-db-engine-versions --engine aurora-postgresql --engine-version 16.10`,
    which reports ServerlessV2FeaturesSupport.MinCapacity of 0.
  EOT
  type        = string
  default     = "16.10"
}

variable "max_capacity" {
  description = <<-EOT
    Aurora Serverless v2 maximum ACUs. Minimum capacity is 0 so the cluster pauses, but
    the maximum cannot also be below 1.0 — AWS rejects 0.5 for both, and the Terraform
    provider validates max_capacity into the range 1.0 to 256.0 before the API sees it.
  EOT
  type        = number
  default     = 1.0
}

variable "seconds_until_auto_pause" {
  description = <<-EOT
    Idle seconds before the cluster pauses. 86400 is the maximum and the point is the
    demo: a paused cluster takes about fifteen seconds to resume, an HTTP API times out
    at thirty, and the recording should never contain that gamble.
  EOT
  type        = number
  default     = 86400
}

variable "lambda_memory_mb" {
  description = <<-EOT
    Lambda memory, which on Lambda also buys CPU. 1024 rather than the 128 default
    because cold start here is importing pydantic and building the MCP tool schemas, and
    that is CPU-bound. Duration is billed in GB-seconds so faster and fatter costs about
    the same, and at demo volume the whole line is rounding error either way.
  EOT
  type        = number
  default     = 1024
}

variable "log_retention_days" {
  description = "CloudWatch retention. Logs are the step-3 tool-call lines; 30 days is plenty and unbounded retention bills forever."
  type        = number
  default     = 30
}

variable "enable_auth" {
  description = <<-EOT
    Whether to create Cognito and put the server behind a bearer token.

    It defaulted to false for the first apply, on purpose: the day-5 gate is a
    deployed server answering a real tool call, and proving that before OAuth went
    on the critical path was the risk ordering the build plan chose. That apply
    happened, the endpoint was proven with curl, and the evidence is in
    docs/evidence/step-5-deployed-tool-call.json.

    The default is now true because that is what is deployed, and a variable whose
    default contradicts the live account is a `terraform apply` away from deleting a
    user pool by accident. Set it false to go back to an open endpoint — which is
    also the fallback if a client turns out not to be able to discover Cognito.
  EOT
  type        = bool
  default     = true
}

variable "oauth_as_mode" {
  description = <<-EOT
    How the server advertises its authorization server. "cognito" points clients at
    Cognito's issuer and depends on them falling back to OIDC discovery, which
    Cognito serves. "self" makes the server publish its own RFC 8414 document
    pointing at Cognito's hosted UI, for clients that only look for the RFC 8414
    path. Try cognito first; it needs no facade at all.
  EOT
  type        = string
  default     = "cognito"

  validation {
    condition     = contains(["cognito", "self"], var.oauth_as_mode)
    error_message = "oauth_as_mode must be \"cognito\" or \"self\"."
  }
}

variable "oauth_callback_urls" {
  description = <<-EOT
    Redirect URIs the Cognito app client will accept, matched exactly. The real one
    is whatever Cowork sends and is read off Cognito's error page on the first
    failed attempt; these are the published Claude connector callbacks as a
    starting point.

    The localhost entry is step 10's, and it is a different client rather than a
    different environment. Cowork redirects to claude.ai; the Claude Code CLI runs
    the same authorization_code flow against a loopback listener and sends
    http://localhost:PORT/callback. Both have to be registered for the plugin
    manifest to be installable in both places, and Cognito exact-matches, so the
    port is pinned here and in plugin/.mcp.json's oauth.callbackPort rather than
    left to the CLI's default of picking a free one.

    http:// is legal here only because the host is localhost; Cognito rejects the
    scheme for any other host.
  EOT
  type        = list(string)
  default = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:8765/callback",
  ]
}

variable "demo_user_email" {
  description = "The one Cognito user that signs in during the demo. Synthetic, like everything else in this repository."
  type        = string
  default     = "demo@bizdata.invalid"
}

variable "lambda_web_adapter_layer" {
  description = <<-EOT
    The Lambda Web Adapter layer. Version 28 is adapter 1.0.1, confirmed readable from
    this account with `aws lambda get-layer-version`. This is what lets server/app.py run
    as the same ASGI app it is locally, with no handler-shaped rewrite.
  EOT
  type        = string
  default     = "arn:aws:lambda:us-west-2:753240598075:layer:LambdaAdapterLayerArm64:28"
}
