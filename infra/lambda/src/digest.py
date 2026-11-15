"""Daily digest: query the sparse index for unsent matches, email via SNS, then mark them sent."""
import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

import scoring
import store

SNS = boto3.client("sns")
TOPIC_ARN = os.environ["TOPIC_ARN"]
INDEX_NAME = os.environ.get("INDEX_NAME", "pending-index")


def format_digest(items):
    lines = [f"{len(items)} new job match(es), best first.", ""]
    for it in items:
        where = f" ({it['location']})" if it.get("location") else ""
        flags = f"  ⚠ {', '.join(it['flags'])}" if it.get("flags") else ""
        lines.append(f"{int(it['score']):>3} · {it['title']} — {it['company']}{where}{flags}")
        lines.append(f"      {', '.join(it.get('matched', [])[:6])}")
        lines.append(f"      {it['url']}")
        lines.append("")
    lines.append("RemoteOK listings link back to their original pages.")
    return "\n".join(lines)


def handler(event, context):
    resp = store.TABLE.query(
        IndexName=INDEX_NAME,
        KeyConditionExpression=Key("digest_pk").eq(store.PENDING),
        ScanIndexForward=False,  # highest score first
        Limit=scoring.PROFILE["max_digest_items"],
    )
    items = resp["Items"]
    if not items:
        print(json.dumps({"sent": 0}))
        return {"sent": 0}

    SNS.publish(
        TopicArn=TOPIC_ARN,
        Subject=f"Job Radar: {len(items)} new match(es), top score {int(items[0]['score'])}"[:100],
        Message=format_digest(items),
    )
    # Publish first, mark second: a failure here can repeat a job tomorrow but never lose one.
    now = datetime.now(timezone.utc).isoformat()
    for it in items:
        store.TABLE.update_item(
            Key={"job_id": it["job_id"]},
            UpdateExpression="REMOVE digest_pk SET notified_at = :t",
            ExpressionAttributeValues={":t": now},
        )
    print(json.dumps({"sent": len(items)}))
    return {"sent": len(items)}
