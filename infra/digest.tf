# Unencrypted on purpose: CloudWatch alarms cannot publish to a topic encrypted with the AWS-managed
# alias/aws/sns key, and the payload is just public job titles. See .checkov.yaml.
resource "aws_sns_topic" "job_radar" {
  name = "job-radar"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.job_radar.arn
  protocol  = "email"
  endpoint  = var.notification_email # AWS emails a confirmation link: click it once
}

resource "aws_cloudwatch_log_group" "digest" {
  name              = "/aws/lambda/job-radar-digest"
  retention_in_days = 14
}

resource "aws_iam_role" "digest" {
  name               = "job-radar-digest"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "digest" {
  statement {
    sid       = "QueryPendingIndex"
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.jobs.arn}/index/pending-index"]
  }
  statement {
    sid       = "MarkNotified"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.jobs.arn]
  }
  statement {
    sid       = "PublishDigest"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.job_radar.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.digest.arn}:*"]
  }
}

resource "aws_iam_role_policy" "digest" {
  name   = "job-radar-digest"
  role   = aws_iam_role.digest.id
  policy = data.aws_iam_policy_document.digest.json
}

resource "aws_lambda_function" "digest" {
  function_name    = "job-radar-digest"
  role             = aws_iam_role.digest.arn
  runtime          = "python3.13"
  handler          = "digest.handler"
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.jobs.name
      TOPIC_ARN  = aws_sns_topic.job_radar.arn
      INDEX_NAME = "pending-index"
    }
  }

  depends_on = [aws_cloudwatch_log_group.digest]
}

resource "aws_cloudwatch_event_rule" "digest" {
  name                = "job-radar-digest-daily"
  description         = "Email the best unsent matches"
  schedule_expression = "cron(30 5 * * ? *)" # 05:30 UTC = 06:30/07:30 in Paris
}

resource "aws_cloudwatch_event_target" "digest" {
  rule = aws_cloudwatch_event_rule.digest.name
  arn  = aws_lambda_function.digest.arn
}

resource "aws_lambda_permission" "digest_schedule" {
  statement_id  = "AllowEventBridgeDigest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.digest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.digest.arn
}

output "sns_topic_arn" {
  value = aws_sns_topic.job_radar.arn
}
