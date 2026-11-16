#!/usr/bin/env python3
"""Export RemoteOK (or all) matches WITH descriptions as one prompt you can paste/upload to Claude.

    source ~/.venvs/jr/bin/activate
    python3 scripts/export_remoteok.py --out matches.md        # then drag matches.md into the chat
    python3 scripts/export_remoteok.py --min-score 60 --days 3 --limit 10

Reads the whole table with a filter, which is fine at this size (and only when you run it).
"""
import argparse
import sys

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config

TABLE_NAME = "job-radar-jobs"


def fetch(table, source, min_score, since_iso, limit):
    cond = Attr("score").gte(min_score) & Attr("ingested_at").gte(since_iso)
    if source != "all":
        cond = cond & Attr("source").eq(source)
    items, kwargs = [], {"FilterExpression": cond}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda it: -it["score"])
    return items[:limit]


def to_prompt(jobs):
    head = f"Write a tailored cover letter for each of the {len(jobs)} job(s) below (job-radar matches, best fit first).\n\n"
    blocks = []
    for i, j in enumerate(jobs, 1):
        lines = [
            f"## {i}. {j['title']} — {j['company']}",
            f"Location: {j.get('location') or 'n/a'} | Fit score: {int(j['score'])}/100 | Matched: {', '.join(j.get('matched', [])) or '-'}",
        ]
        if j.get("flags"):
            lines.append(f"Flags: {', '.join(j['flags'])}")
        lines += [f"URL: {j['url']}", "", (j.get("description") or "").strip()]
        blocks.append("\n".join(lines))
    return head + "\n\n---\n\n".join(blocks)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="remoteok", help="remoteok (default), linkedin-extension or all")
    ap.add_argument("--min-score", type=int, default=45)
    ap.add_argument("--days", type=int, default=7, help="only jobs ingested in the last N days")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--out", help="write to this file instead of stdout")
    args = ap.parse_args()

    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    table = boto3.resource("dynamodb", config=Config(retries={"max_attempts": 10, "mode": "adaptive"})).Table(TABLE_NAME)
    jobs = fetch(table, args.source, args.min_score, since, args.limit)
    if not jobs:
        print("No matching jobs.", file=sys.stderr)
        return 1
    text = to_prompt(jobs)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {len(jobs)} job(s), {len(text):,} characters to {args.out}", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
