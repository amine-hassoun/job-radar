# PROVISIONED on purpose: DynamoDB's 25 WCU / 25 RCU Always Free allowance is ONE pool shared
# by every table in the account/region. This project uses 4 RCU / 6 WCU (table 2/4 + two indexes at 1/1).
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
    name = "captured_pk"
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

  # Second sparse index: only jobs captured from LinkedIn (captured_pk = "CAPTURED"), sorted by score.
  # Powers the extension's list / "Captured" button state; the item disappears from it when deleted.
  # Small projection on purpose: descriptions are fetched on demand with BatchGetItem.
  global_secondary_index {
    name               = "captured-index"
    hash_key           = "captured_pk"
    range_key          = "score"
    projection_type    = "INCLUDE"
    non_key_attributes = ["title", "company", "location", "url", "source", "matched", "flags", "ingested_at"]
    read_capacity      = 1
    write_capacity     = 1
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
