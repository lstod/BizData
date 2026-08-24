# ---------------------------------------------------------------------------
# The MCP server, as a Lambda with no VPC configuration at all.
#
# Two things make this the same server that runs locally rather than a port of
# it. The Lambda Web Adapter layer turns the invocation into a real HTTP request
# against a real HTTP server, so server/app.py is served by uvicorn here exactly
# as it is on a laptop — no handler function, no ASGI shim, no `if
# running_in_lambda` branch anywhere in the codebase. And BIZDATA_DB_BACKEND
# selects the Data API backend behind the same interface the local one
# implements, which is the seam step 3 built for precisely this moment.
#
# Note the absence: there is no vpc_config block. That is the design, and
# `aws lambda get-function-configuration` showing no VpcConfig is one of step
# 5's Done-when conditions.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "bizdata-mcp-server"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The whole of the server's authority over data, and it is worth reading as a
# list of what it cannot do. There is no rds:*, so it cannot alter the cluster.
# There is no secretsmanager entry for the master secret, so it cannot obtain a
# credential that writes. The Data API actions it does hold are meaningless
# without a secret, and the only secret it can read belongs to a Postgres role
# with SELECT and nothing else.
data "aws_iam_policy_document" "lambda" {
  statement {
    sid    = "DataApiAgainstTheOneCluster"
    effect = "Allow"

    actions = [
      "rds-data:ExecuteStatement",
      "rds-data:BatchExecuteStatement",
    ]

    resources = [aws_rds_cluster.main.arn]
  }

  statement {
    sid       = "ReadTheReadOnlySecretAndNoOther"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.mcp_readonly.arn]
  }

  # Step 9. The server's only write, and it is narrower than it looks: PutObject
  # under one prefix of one bucket. No DeleteObject, so nothing already archived
  # can be removed; no ListBucket, so it cannot enumerate other runs; no bucket
  # ACL or policy actions, so it cannot widen its own access.
  #
  # This statement is also the second fence around key confinement. The key is
  # built in server/tools/publish_pack.py from a validated run id and a validated
  # bare filename, and a presigned URL inherits the signer's permissions — so a
  # key that escaped that validation would still be refused here. Two independent
  # mechanisms, because the first one is code and the second one is not.
  statement {
    sid       = "WriteTheRunArchiveAndNothingElse"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.runs.arn}/runs/*"]
  }

  # HeadObject is authorised as GetObject, and head is how publish_pack finds out
  # whether the artifacts it is about to vouch for actually arrived. Without this
  # the ledger entry could only ever record an intention.
  statement {
    sid       = "ReadBackWhatWasJustWritten"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.runs.arn}/runs/*"]
  }

  # The tool-call log is the server's own record, so the server gathers it rather
  # than trusting the agent's account of its own calls. AWSLambdaBasicExecutionRole
  # grants CreateLogStream and PutLogEvents but not FilterLogEvents, so writing to
  # the log group does not imply being able to read it back and this is a real
  # addition rather than a redundant one. Scoped to this function's own group.
  statement {
    sid       = "ReadThisFunctionsOwnToolCallLog"
    effect    = "Allow"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "bizdata-mcp-data-access"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# Created here rather than left to Lambda, which would make it on first
# invocation with retention set to never expire.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/bizdata-mcp-server"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "mcp" {
  function_name = "bizdata-mcp-server"
  role          = aws_iam_role.lambda.arn

  # Built by scripts/package_lambda.sh, which produces a byte-identical archive
  # from unchanged sources so that source_code_hash only moves when the code
  # does. An apply that always reports a change is an apply nobody reads.
  filename         = "${path.module}/../../build/bizdata-server.zip"
  source_code_hash = filebase64sha256("${path.module}/../../build/bizdata-server.zip")

  runtime       = "python3.12"
  architectures = ["arm64"]

  # The handler is a shell script, which is the Lambda Web Adapter's contract:
  # /opt/bootstrap execs it, and it starts uvicorn.
  handler = "run.sh"

  layers = [var.lambda_web_adapter_layer]

  memory_size = var.lambda_memory_mb

  # The API Gateway HTTP API gives up at 30 seconds, so a longer function
  # timeout could only ever produce a client that has already gone away.
  timeout = 30

  environment {
    variables = merge(
      {
        BIZDATA_DB_BACKEND  = "aws"
        BIZDATA_CLUSTER_ARN = aws_rds_cluster.main.arn
        BIZDATA_SECRET_ARN  = aws_secretsmanager_secret.mcp_readonly.arn
        BIZDATA_DATABASE    = var.db_name

        # Step 9's two seams, selected the same way BIZDATA_DB_BACKEND selects
        # the database. Both default to their local implementations when unset,
        # so `uvicorn server.app:app` on a laptop still needs no AWS account.
        BIZDATA_ARCHIVE_BACKEND = "s3"
        BIZDATA_RUNS_BUCKET     = aws_s3_bucket.runs.id
        BIZDATA_RUNLOG_BACKEND  = "cloudwatch"
        BIZDATA_LOG_GROUP       = aws_cloudwatch_log_group.lambda.name

        # The adapter itself. AWS_LAMBDA_EXEC_WRAPPER is what activates the
        # layer at all.
        AWS_LAMBDA_EXEC_WRAPPER = "/opt/bootstrap"
        AWS_LWA_PORT            = "8000"

        # TCP rather than the default HTTP readiness probe. The HTTP probe polls
        # "/", and this app has no route there — it serves /mcp and the OAuth
        # well-known paths and nothing else — so an HTTP probe is asking a
        # question whose only honest answer is 404.
        AWS_LWA_READINESS_CHECK_PROTOCOL = "tcp"
      },
      local.lambda_auth_env,
    )
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_cloudwatch_log_group.lambda,
  ]
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowExecutionFromHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
