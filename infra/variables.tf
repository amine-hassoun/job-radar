variable "notification_email" {
  description = "Where the daily digest and error alarms are emailed. Set in terraform.tfvars (gitignored)."
  type        = string
}

variable "aws_region" {
  description = "AWS region to deploy to. The extension's host permission must match it (scripts/quickstart.sh patches manifest.json)."
  type        = string
  default     = "eu-west-3"
}
