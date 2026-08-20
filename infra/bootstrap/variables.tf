variable "region" {
  description = "Region for all BizData resources."
  type        = string
  default     = "us-west-2"
}

variable "alert_email" {
  description = "Address that receives budget alerts. The SNS subscription it creates must be confirmed from the inbox before the alarm is real."
  type        = string
}

variable "budget_limit_usd" {
  description = "Account-wide monthly cost ceiling, in USD."
  type        = string
  default     = "20"
}

variable "actual_alert_thresholds" {
  description = "Percentages of the budget at which an alert on actual spend fires."
  type        = list(number)
  default     = [50, 80, 100]
}
