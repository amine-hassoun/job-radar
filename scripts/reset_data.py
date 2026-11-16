#!/usr/bin/env python3
"""Erase EVERY item in the job-radar jobs table so scanning starts from zero.

    source ~/.venvs/jr/bin/activate && python3 scripts/reset_data.py

Shows what is about to go and asks you to type DELETE. The table, its indexes and all infrastructure stay.
Also clear the extension's local cache afterwards (see README, "Reset").
"""
import sys
from collections import Counter

import boto3
from botocore.config import Config

TABLE_NAME = "job-radar-jobs"


def all_keys(table):
    keys, kwargs = [], {"ProjectionExpression": "job_id"}
    while True:
        page = table.scan(**kwargs)
        keys += [item["job_id"] for item in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return keys
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def wipe(table, keys):
    # batch_writer groups deletes by 25 and retries unprocessed items (the table has tiny provisioned throughput)
    with table.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key={"job_id": key})
    return len(keys)


def main():
    table = boto3.resource("dynamodb", config=Config(retries={"max_attempts": 10, "mode": "adaptive"})).Table(TABLE_NAME)
    keys = all_keys(table)
    by_source = Counter(key.split("#", 1)[0] for key in keys)
    print(f"{len(keys)} item(s) in {TABLE_NAME}: {dict(by_source) or 'empty'}")
    if not keys:
        return 0
    if input("Type DELETE to erase all of them: ").strip() != "DELETE":
        print("Aborted, nothing deleted.")
        return 1
    print(f"Deleted {wipe(table, keys)} item(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
