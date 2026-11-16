"""Shared DynamoDB access: build a scored item, write it only if new."""
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import scoring

# Provisioned throughput is tiny (Always Free pool), so let boto3 back off and retry on throttling.
DDB = boto3.resource("dynamodb", config=Config(retries={"max_attempts": 10, "mode": "adaptive"}))
TABLE = DDB.Table(os.environ["TABLE_NAME"])

TTL_SECONDS = 30 * 24 * 3600
DESCRIPTION_MAX = 4000  # keeps item size (and WCU per write) small
PENDING = "PENDING"  # value of the sparse-index key: present only while a job awaits the digest
CAPTURED = "CAPTURED"  # sparse-index key for jobs captured from LinkedIn, until I delete them after applying


def build_item(*, source, ext_id, title, company, location, description, url, similarity,
               remote_hint=False, captured=False, description_max=DESCRIPTION_MAX):
    result = scoring.score_job(title, description, location, similarity, remote_hint)
    item = {
        "job_id": f"{source}#{ext_id}",
        "source": source,
        "title": title,
        "company": company,
        "description": description[:description_max],
        "url": url,
        "score": result["score"],
        "matched": result["matched"],
        "flags": result["flags"],
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": int(time.time()) + TTL_SECONDS,
    }
    if location:
        item["location"] = location
    if captured:
        item["captured_pk"] = CAPTURED
    if result["score"] >= scoring.PROFILE["digest_min_score"]:
        item["digest_pk"] = PENDING
    return item


def put_new(item):
    """True if stored, False if the job already existed. Other errors propagate."""
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
