resource "aws_cloudwatch_log_group" "fetch" {
  name              = "/aws/lambda/job-radar-fetch"
  retention_in_days = 14
}

resource "aws_iam_role" "fetch" {
  name               = "job-radar-fetch"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "fetch" {
  statement {
    sid       = "WriteJobs"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.jobs.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.fetch.arn}:*"]
  }
}

resource "aws_iam_role_policy" "fetch" {
  name   = "job-radar-fetch"
  role   = aws_iam_role.fetch.id
  policy = data.aws_iam_policy_document.fetch.json
}

# Deliberately NOT VPC-attached: it calls public APIs, and a VPC would need a NAT Gateway (not Always Free).
resource "aws_lambda_function" "fetch" {
  function_name    = "job-radar-fetch"
  role             = aws_iam_role.fetch.arn
  runtime          = "python3.13"
  handler          = "fetch.handler"
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.jobs.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.fetch]
}

resource "aws_cloudwatch_event_rule" "fetch" {
  name                = "job-radar-fetch-daily"
  description         = "Pull RemoteOK, score, store"
  schedule_expression = "cron(0 4 * * ? *)" # 04:00 UTC, an hour and a half before the digest
}

resource "aws_cloudwatch_event_target" "fetch" {
  rule = aws_cloudwatch_event_rule.fetch.name
  arn  = aws_lambda_function.fetch.arn
}

resource "aws_lambda_permission" "fetch_schedule" {
  statement_id  = "AllowEventBridgeFetch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fetch.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.fetch.arn
}
