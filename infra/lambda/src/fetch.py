"""Scheduled fetcher: RemoteOK -> score -> store. NOT VPC-attached (needs free internet egress)."""
import html
import json
import re
import urllib.request

import scoring
import store

USER_AGENT = "job-radar/1.0 (personal job-search tool)"
_TAGS = re.compile(r"<[^>]+>")


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URLs only, built below)
        return json.loads(resp.read().decode("utf-8"))


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", text or ""))).strip()


def fetch_remoteok(errors):
    try:
        data = http_get_json("https://remoteok.com/api", timeout=20)
    except Exception as err:
        errors.append(f"remoteok: {type(err).__name__}")
        return []
    jobs = []
    for r in data:
        if not isinstance(r, dict) or not r.get("id"):  # element 0 is a legal notice, not a job
            continue
        jobs.append({
            "source": "remoteok",
            "ext_id": str(r["id"]),
            "title": clean(r.get("position")),
            "company": clean(r.get("company")),
            "location": clean(r.get("location")) or "Remote",
            "description": clean(r.get("description")),
            "url": r.get("url") or "",  # RemoteOK requires linking back to the original listing
            "remote": True,
        })
    return jobs


def handler(event, context):
    errors = []
    raw = fetch_remoteok(errors)
    if not raw and errors:
        raise RuntimeError(f"fetch failed: {errors}")  # raises -> Errors metric -> alarm -> email

    sims = scoring.similarities([j["description"] for j in raw])
    stored = duplicates = skipped = 0
    seen = set()
    for job, sim in zip(raw, sims):
        if not job["title"] or not job["company"] or not job["url"] or len(job["description"]) < 30:
            skipped += 1
            continue
        item = store.build_item(
            source=job["source"], ext_id=job["ext_id"], title=job["title"], company=job["company"],
            location=job["location"], description=job["description"], url=job["url"],
            similarity=sim, remote_hint=job["remote"],
        )
        if item["job_id"] in seen:
            duplicates += 1
            continue
        seen.add(item["job_id"])
        if item["score"] < scoring.PROFILE["store_min_score"]:
            skipped += 1
            continue
        if store.put_new(item):
            stored += 1
        else:
            duplicates += 1

    summary = {"fetched": len(raw), "stored": stored, "duplicates": duplicates, "skipped": skipped, "errors": errors}
    print(json.dumps(summary))
    return summary
