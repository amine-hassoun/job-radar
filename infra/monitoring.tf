# 3 alarms. Free tier covers 10 alarms per account (project 2 already uses 3).
locals {
  functions = {
    ingest = aws_lambda_function.ingest.function_name
    fetch  = aws_lambda_function.fetch.function_name
    digest = aws_lambda_function.digest.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  for_each = local.functions

  alarm_name          = "job-radar-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.job_radar.arn]
}
