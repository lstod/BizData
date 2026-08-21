# ---------------------------------------------------------------------------
# Cognito, which is step 14 pulled forward into step 5.
#
# Everything here is behind var.enable_auth, and the toggle is the risk ordering
# rather than indecision. The build applies once with enable_auth = false to
# prove the deployment — Aurora, the Data API, the no-VPC Lambda, a real tool
# call from Cowork — and only then applies again with it true. Auth on the
# critical path before the deployment is proven is how a day-5 gate is missed,
# and a variable makes the two-stage sequence explicit instead of relying on
# somebody remembering to comment a file out.
#
# Step 4 established the two facts this file is shaped by. Cowork's connector
# form offers "OAuth Client ID (optional)" and "OAuth Client Secret (optional)",
# so a pre-registered client works and dynamic client registration stays
# unbuilt — which is what it deserves, being deprecated in favour of client ID
# metadata documents. And the form offers no authorization endpoint field, no
# token endpoint field and no scopes field, so Cowork must discover all three
# from the server URL. That discovery chain is the risk in this file, and it is
# handled in server/auth.py rather than here.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "main" {
  count = var.enable_auth ? 1 : 0

  name = "bizdata"

  # Nobody self-registers into a demo authorization server.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 16
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  # Email as the sign-in identifier, because "Individual sign-in — each member
  # signs in to connect" is what Cowork's form says it will ask for.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  count = var.enable_auth ? 1 : 0

  # Globally unique across all of Cognito, hence the account id.
  domain       = "bizdata-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main[0].id
}

# A resource server exists to make `bizdata/read` a real scope rather than a
# string nobody checks. Without one, the only scopes available are the OIDC
# built-ins, and the server would be reduced to asserting that a token exists
# rather than that it grants anything in particular.
resource "aws_cognito_resource_server" "bizdata" {
  count = var.enable_auth ? 1 : 0

  identifier   = "bizdata"
  name         = "BizData MCP server"
  user_pool_id = aws_cognito_user_pool.main[0].id

  scope {
    scope_name        = "read"
    scope_description = "Read the delivery and margin data layer."
  }
}

resource "aws_cognito_user_pool_client" "cowork" {
  count = var.enable_auth ? 1 : 0

  name         = "cowork-connector"
  user_pool_id = aws_cognito_user_pool.main[0].id

  # The two fields Cowork's Advanced settings offers. Created once, as code,
  # which is the entire reason step 14 collapsed.
  generate_secret = true

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]

  allowed_oauth_scopes = concat(
    ["openid"],
    [for s in aws_cognito_resource_server.bizdata[0].scope_identifiers : s],
  )

  supported_identity_providers = ["COGNITO"]

  # Exact-match, per RFC 6749 §3.1.2.3, and Cognito enforces it strictly. The
  # real value is whatever Cowork sends, which is only learnable by watching a
  # failed attempt — Cognito names the offending redirect_uri in the error. The
  # defaults below are the published Claude connector callbacks; the variable
  # exists so the observed one can be added without editing this file.
  callback_urls = var.oauth_callback_urls

  # Tokens live long enough for a demo and no longer.
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  enable_token_revocation = true

  # Cognito otherwise answers a bad username and a bad password differently,
  # which is a free user-enumeration oracle on a public endpoint.
  prevent_user_existence_errors = "ENABLED"
}

# A second client, for proving the server rather than for using it.
#
# The connector's flow is authorization_code with PKCE, which requires a human
# at a browser and therefore cannot be asserted in a script. That leaves the
# server's own half of the contract — signature, issuer, token_use, client_id,
# expiry, scope — untested by anything repeatable, which for the one security
# boundary in the build is the wrong place to have no test.
#
# client_credentials closes that. It is the one Cognito grant that mints a real
# access token, signed by the real pool, carrying the real bizdata/read scope,
# with no browser anywhere. scripts/check_auth.py uses it to prove the chain end
# to end and to prove the negatives that matter more: no token is refused, a
# tampered token is refused, and an ID token is refused.
#
# It is deliberately not the connector's client. Cowork gets a client that can
# only do authorization_code, so the demo path and the test path cannot be
# confused for one another.
resource "aws_cognito_user_pool_client" "machine" {
  count = var.enable_auth ? 1 : 0

  name         = "bizdata-test-machine"
  user_pool_id = aws_cognito_user_pool.main[0].id

  generate_secret = true

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]

  # No openid here, and not by preference: client_credentials has no user, so
  # there is no identity to describe and Cognito rejects the combination.
  allowed_oauth_scopes = aws_cognito_resource_server.bizdata[0].scope_identifiers

  access_token_validity = 60

  token_validity_units {
    access_token = "minutes"
  }
}

# Someone has to be able to sign in. "Individual sign-in" is Cowork's model, so
# a client-credentials machine token would not exercise the flow the connector
# actually performs.
resource "random_password" "demo_user" {
  count = var.enable_auth ? 1 : 0

  length           = 24
  special          = true
  override_special = "!@#$%^&*()-_=+"
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
}

resource "aws_cognito_user" "demo" {
  count = var.enable_auth ? 1 : 0

  user_pool_id = aws_cognito_user_pool.main[0].id
  username     = var.demo_user_email

  attributes = {
    email          = var.demo_user_email
    email_verified = true
  }

  password       = random_password.demo_user[0].result
  message_action = "SUPPRESS"
}

# ---------------------------------------------------------------------------
# What the Lambda is told about all this.
#
# server/auth.py reads these and stays inert when BIZDATA_AUTH is absent, which
# is what keeps `uvicorn server.app:app` working on a laptop with no AWS at all.
#
# BIZDATA_OAUTH_AS_MODE is the discovery decision, and it is a variable because
# the answer is not knowable from the documentation. In "cognito" mode the
# server advertises Cognito itself as the authorization server and relies on the
# client falling back to /.well-known/openid-configuration, which Cognito does
# serve. In "self" mode the server advertises itself and serves an RFC 8414
# document pointing at Cognito's hosted UI, because Cognito does not serve RFC
# 8414 at the path the MCP flow looks for. Cheapest first; flip if it fails.
# ---------------------------------------------------------------------------

locals {
  cognito_issuer = var.enable_auth ? "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main[0].id}" : ""
  cognito_domain = var.enable_auth ? "https://${aws_cognito_user_pool_domain.main[0].domain}.auth.${var.region}.amazoncognito.com" : ""

  lambda_auth_env = var.enable_auth ? {
    BIZDATA_AUTH           = "cognito"
    BIZDATA_OAUTH_ISSUER   = local.cognito_issuer
    BIZDATA_OAUTH_JWKS_URL = "${local.cognito_issuer}/.well-known/jwks.json"

    # Both clients, comma separated. A token is only accepted if it was issued to
    # one of them, so a correctly-signed token minted for some other app in the
    # same pool is still rejected.
    BIZDATA_OAUTH_CLIENT_ID = join(",", [
      aws_cognito_user_pool_client.cowork[0].id,
      aws_cognito_user_pool_client.machine[0].id,
    ])
    BIZDATA_OAUTH_SCOPES = join(",", aws_cognito_resource_server.bizdata[0].scope_identifiers)

    BIZDATA_RESOURCE_URL = "${aws_apigatewayv2_api.main.api_endpoint}/mcp"

    BIZDATA_OAUTH_AS_MODE                = var.oauth_as_mode
    BIZDATA_OAUTH_AUTHORIZATION_ENDPOINT = "${local.cognito_domain}/oauth2/authorize"
    BIZDATA_OAUTH_TOKEN_ENDPOINT         = "${local.cognito_domain}/oauth2/token"
    BIZDATA_OAUTH_USERINFO_ENDPOINT      = "${local.cognito_domain}/oauth2/userInfo"
    BIZDATA_OAUTH_REVOCATION_ENDPOINT    = "${local.cognito_domain}/oauth2/revoke"
  } : {}
}
