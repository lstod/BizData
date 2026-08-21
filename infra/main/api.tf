# ---------------------------------------------------------------------------
# The HTTP API in front of the server.
#
# Two choices here are about OAuth rather than about HTTP, and both would be
# painful to change later.
#
# The stage is $default. An HTTP API's named stage puts the stage into the path
# — https://<id>.execute-api.../prod/mcp — and OAuth discovery is specified in
# terms of the host root: RFC 9728 puts protected resource metadata at
# /.well-known/oauth-protected-resource, and no client will look under /prod for
# it. $default removes the prefix, so the well-known paths land where the spec
# says they are. This is the single most common way the Cognito-plus-MCP
# integration is reported broken.
#
# The route is $default too, so every path reaches the Lambda and the routing
# decision belongs to Starlette. That keeps one router rather than two: the SDK
# already decides what /mcp and the well-known paths do, and duplicating that
# list in Terraform would mean adding a route here every time the auth module
# starts serving a new document.
#
# There is deliberately no JWT authorizer on this API even after Cognito lands.
# API Gateway's own 401 is a bare {"message":"Unauthorized"} with no
# WWW-Authenticate header, and an MCP client discovers where to authenticate
# from exactly that header — `WWW-Authenticate: Bearer resource_metadata="..."`.
# Authorising in the app means the SDK's RequireAuthMiddleware writes that
# header, which is what makes the connector able to start a flow at all. An
# authorizer here would be cheaper per request and would break discovery.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "main" {
  name          = "bizdata-mcp"
  description   = "MCP server for the BizData delivery and margin review."
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id = aws_apigatewayv2_api.main.id

  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.mcp.invoke_arn

  # 2.0 is what the Lambda Web Adapter expects and what carries the request as
  # an HTTP-shaped event rather than a REST-era one.
  payload_format_version = "2.0"

  # The API's own ceiling. Slightly under the 30 second hard limit so the
  # gateway, not the client, is the thing that reports a timeout.
  timeout_milliseconds = 29000
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/bizdata-mcp"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn

    # No request headers here, and that is a limitation rather than a choice.
    # "Which MCP protocol revision does Cowork speak?" is one of the four
    # build-time verifications and the answer lives in the first successful
    # request's Mcp-Protocol-Version header, so this format originally logged
    # it. An HTTP API rejects that at CreateStage:
    #
    #   BadRequestException: The following context variables are not supported:
    #   [$context.request.header.Mcp-Protocol-Version]
    #
    # $context.request.header.* is a REST API feature. Rather than move the
    # whole API to REST for one header, the header is logged by the application
    # in server/reqlog.py, which lands in the same CloudWatch and carries the
    # negotiated protocol version, the user agent and whether the request
    # arrived authenticated.
    format = jsonencode({
      requestId         = "$context.requestId"
      requestTime       = "$context.requestTime"
      httpMethod        = "$context.httpMethod"
      path              = "$context.path"
      status            = "$context.status"
      responseLatency   = "$context.responseLatency"
      integrationStatus = "$context.integrationStatus"
      integrationError  = "$context.integrationErrorMessage"
      userAgent         = "$context.identity.userAgent"
      sourceIp          = "$context.identity.sourceIp"
    })
  }

  default_route_settings {
    # A publicly reachable endpoint with no authentication in front of it during
    # phase one, so a throttle is the difference between a surprise and a
    # surprise on the bill. Generous for a demo, meaningless for abuse.
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}
