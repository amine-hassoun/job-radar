"""The extension's API behind one Function URL (shared-secret auth).

Body without "action": ingest {"jobs": [...]}.  With "action": list | export | delete (captured jobs only).
"""
import base64
import hmac
import json
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import scoring
import store

SSM = boto3.client("ssm")
SECRET_PARAM = os.environ["SECRET_PARAM_NAME"]

SOURCE = "linkedin-extension"
MAX_BATCH = 100
MAX_IDS = 100
DESCRIPTION_MAX = 12000  # captured jobs keep the full text: it is what the cover letter is written from
CAPTURED_INDEX = os.environ.get("CAPTURED_INDEX", "captured-index")
JOB_ID_RE = re.compile(r"^\d{5,20}$")

_secret = None


def get_secret():
    global _secret
    if _secret is None:
        _secret = SSM.get_parameter(Name=SECRET_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _secret


def _json_default(value):
    if isinstance(value, Decimal):  # DynamoDB returns numbers as Decimal
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not serializable: {type(value)}")


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def to_item(job):
    if not isinstance(job, dict):
        return None
    job_id = str(job.get("jobId", "")).strip()
    title = str(job.get("title", "")).strip()[:300]
    company = str(job.get("company", "")).strip()[:200]
    description = str(job.get("description", "")).strip()[:15000]
    if not JOB_ID_RE.match(job_id) or not title or not company or len(description) < 100:
        return None
    location = str(job.get("location") or "").strip()[:200]
    return store.build_item(
        source=SOURCE,
        ext_id=job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        similarity=scoring.similarities([description])[0],
        captured=True,
        description_max=DESCRIPTION_MAX,
    )


def parse_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    return body


def job_ids(body):
    """Validated, de-duplicated list of LinkedIn job ids from body["job_ids"], or None."""
    ids = body.get("job_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS:
        return None
    ids = [str(i) for i in ids]
    if not all(JOB_ID_RE.match(i) for i in ids):
        return None
    return list(dict.fromkeys(ids))


def ingest_jobs(body):
    jobs = body.get("jobs")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_BATCH:
        return reply(400, {"error": 'expected JSON {"jobs": [1-100 items]}'})

    stored, duplicates, rejected, scores = 0, 0, [], []
    for index, job in enumerate(jobs):
        item = to_item(job)
        if item is None:
            rejected.append(index)
            continue
        try:
            if store.put_new(item):
                stored += 1
                scores.append(item["score"])
            else:
                duplicates += 1
        except ClientError as err:
            print(json.dumps({"error": err.response["Error"]["Code"], "job_id": item["job_id"]}))
            return reply(500, {"error": "storage_error"})

    return reply(200, {"stored": stored, "duplicates": duplicates, "rejected": rejected, "scores": scores})


def api_list(body):
    """Captured jobs, best score first. Reads the sparse index (small projection): cheap on 1 RCU."""
    min_score = scoring.PROFILE["digest_min_score"] if body.get("only_fits") else 0
    resp = store.TABLE.query(
        IndexName=CAPTURED_INDEX,
        KeyConditionExpression=Key("captured_pk").eq(store.CAPTURED) & Key("score").gte(min_score),
        ScanIndexForward=False,
        Limit=200,
    )
    jobs = [
        {
            "jobId": it["job_id"].split("#", 1)[1],
            "title": it.get("title"),
            "company": it.get("company"),
            "location": it.get("location"),
            "url": it.get("url"),
            "score": it.get("score"),
            "matched": it.get("matched", []),
            "flags": it.get("flags", []),
            "ingestedAt": it.get("ingested_at"),
        }
        for it in resp["Items"]
    ]
    return reply(200, {"jobs": jobs, "min_score": min_score})


def api_export(body):
    """Full records (with descriptions) for the chosen ids, to paste into Claude."""
    ids = job_ids(body)
    if ids is None:
        return reply(400, {"error": 'expected {"job_ids": [1-100 numeric ids]}'})
    request = {store.TABLE.name: {"Keys": [{"job_id": f"{SOURCE}#{i}"} for i in ids]}}
    items = []
    for _ in range(5):  # BatchGetItem may return UnprocessedKeys under throttling
        resp = store.DDB.batch_get_item(RequestItems=request)
        items += resp["Responses"].get(store.TABLE.name, [])
        request = resp.get("UnprocessedKeys") or {}
        if not request:
            break
    items.sort(key=lambda it: -it.get("score", 0))
    jobs = [
        {
            "jobId": it["job_id"].split("#", 1)[1],
            "title": it.get("title"),
            "company": it.get("company"),
            "location": it.get("location"),
            "url": it.get("url"),
            "score": it.get("score"),
            "matched": it.get("matched", []),
            "flags": it.get("flags", []),
            "description": it.get("description", ""),
        }
        for it in items
    ]
    return reply(200, {"jobs": jobs})


def api_delete(body):
    """Delete captured jobs by id. Keys are built server-side, so only linkedin-extension items can go."""
    ids = job_ids(body)
    if ids is None:
        return reply(400, {"error": 'expected {"job_ids": [1-100 numeric ids]}'})
    for i in ids:
        store.TABLE.delete_item(Key={"job_id": f"{SOURCE}#{i}"})  # idempotent
    return reply(200, {"deleted": len(ids)})


ACTIONS = {"list": api_list, "export": api_export, "delete": api_delete}


def handler(event, context):
    sent = (event.get("headers") or {}).get("x-job-radar-key", "")
    if not hmac.compare_digest(sent.encode(), get_secret().encode()):
        return reply(401, {"error": "unauthorized"})

    try:
        body = parse_body(event)
    except (ValueError, TypeError):
        return reply(400, {"error": 'expected JSON {"jobs": [...]} or {"action": ...}'})

    action = body.get("action")
    if action is None:
        return ingest_jobs(body)
    if action not in ACTIONS:
        return reply(400, {"error": f"unknown action; use one of {sorted(ACTIONS)}"})
    try:
        return ACTIONS[action](body)
    except ClientError as err:
        print(json.dumps({"error": err.response["Error"]["Code"], "action": action}))
        return reply(500, {"error": "storage_error"})
