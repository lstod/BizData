data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Budget alerting
#
# Applied on its own, before any other resource in the account:
#
#   terraform apply \
#     -target=aws_sns_topic.budget_alerts \
#     -target=aws_sns_topic_policy.budget_alerts \
#     -target=aws_sns_topic_subscription.budget_email \
#     -target=aws_budgets_budget.monthly
#
# Alerts go through SNS rather than a bare email subscriber on the budget. A
# bare subscriber sends nothing until the threshold is crossed, which leaves
# no way to prove on day one that alerts reach a human. SNS sends a
# confirmation email immediately, so the delivery path gets tested up front.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "budget_alerts" {
  name = "bizdata-budget-alerts"
}

data "aws_iam_policy_document" "budget_alerts" {
  statement {
    sid       = "AWSBudgetsSNSPublishingPermissions"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.budget_alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_sns_topic_policy" "budget_alerts" {
  arn    = aws_sns_topic.budget_alerts.arn
  policy = data.aws_iam_policy_document.budget_alerts.json
}

resource "aws_sns_topic_subscription" "budget_email" {
  topic_arn = aws_sns_topic.budget_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_budgets_budget" "monthly" {
  name         = "bizdata-monthly"
  budget_type  = "COST"
  limit_amount = var.budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = var.actual_alert_thresholds

    content {
      comparison_operator       = "GREATER_THAN"
      threshold                 = notification.value
      threshold_type            = "PERCENTAGE"
      notification_type         = "ACTUAL"
      subscriber_sns_topic_arns = [aws_sns_topic.budget_alerts.arn]
    }
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.budget_alerts.arn]
  }

  # Budgets verifies it can publish to the topic when the budget is created,
  # so the topic policy has to exist first or creation fails with
  # "Invalid SNS topic".
  depends_on = [aws_sns_topic_policy.budget_alerts]
}

# ---------------------------------------------------------------------------
# Terraform state backend
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tfstate" {
  bucket = "bizdata-tfstate-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
