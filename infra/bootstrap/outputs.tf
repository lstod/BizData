output "account_id" {
  description = "The account everything is built in."
  value       = data.aws_caller_identity.current.account_id
}

output "state_bucket" {
  description = "Bucket name to put in infra/main/backend.hcl."
  value       = aws_s3_bucket.tfstate.bucket
}

output "budget_alert_topic_arn" {
  description = "Topic the budget publishes to. Check its subscription is confirmed, not pending."
  value       = aws_sns_topic.budget_alerts.arn
}
