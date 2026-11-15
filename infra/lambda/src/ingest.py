"""POST endpoint for the Chrome extension: validate, score, store."""
import base64
import hmac
import json
import os
import re

import boto3
from botocore.exceptions import ClientError

import scoring
import store

SSM = boto3.client("ssm")
SECRET_PARAM = os.environ["SECRET_PARAM_NAME"]

SOURCE = "linkedin-extension"
MAX_BATCH = 100
JOB_ID_RE = re.compile(r"^\d{5,20}$")

_secret = None


def get_secret():
    global _secret
    if _secret is None:
        _secret = SSM.get_parameter(Name=SECRET_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _secret


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
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
    )


def handler(event, context):
    sent = (event.get("headers") or {}).get("x-job-radar-key", "")
    if not hmac.compare_digest(sent.encode(), get_secret().encode()):
        return reply(401, {"error": "unauthorized"})

    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        jobs = json.loads(raw)["jobs"]
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_BATCH:
            raise ValueError("bad batch size")
    except (ValueError, KeyError, TypeError):
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
