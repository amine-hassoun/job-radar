# PROVISIONED on purpose: DynamoDB's 25 WCU / 25 RCU Always Free allowance is ONE pool shared
# by every table in the account/region. This project uses 3 RCU / 5 WCU (table 2/4 + index 1/1).
resource "aws_dynamodb_table" "jobs" {
  name           = "job-radar-jobs"
  billing_mode   = "PROVISIONED"
  read_capacity  = 2
  write_capacity = 4
  hash_key       = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # Only needed because the index below keys on them.
  attribute {
    name = "digest_pk"
    type = "S"
  }

  attribute {
    name = "score"
    type = "N"
  }

  # Sparse index: items only appear here while they carry digest_pk = "PENDING"
  # (score >= threshold and not yet emailed). The digest reads this tiny index
  # instead of scanning the table, which keeps RCU use near zero.
  global_secondary_index {
    name               = "pending-index"
    hash_key           = "digest_pk"
    range_key          = "score"
    projection_type    = "INCLUDE"
    non_key_attributes = ["title", "company", "location", "url", "source", "matched", "flags"]
    read_capacity      = 1
    write_capacity     = 1
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
