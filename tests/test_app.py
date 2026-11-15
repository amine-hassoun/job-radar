import json

from conftest import LONG_DESC


def event(jobs, key="s3cret"):
    return {"headers": {"x-job-radar-key": key}, "body": json.dumps({"jobs": jobs})}


def job(job_id="1234567890", title="DevOps Engineer", desc=LONG_DESC):
    return {"jobId": job_id, "title": title, "company": "Acme", "location": "Paris, France", "description": desc}


# ---- scoring ----
def test_good_job_scores_high(aws):
    sim = aws.scoring.similarities([LONG_DESC])[0]
    r = aws.scoring.score_job("Platform Engineer", LONG_DESC, "Remote, Europe", sim)
    assert r["score"] >= 70 and "terraform" in r["matched"] and not r["flags"]


def test_irrelevant_job_scores_low(aws):
    desc = "Senior Java backend developer building Spring microservices for banking. Oracle, Maven, Jenkins."
    sim = aws.scoring.similarities([desc])[0]
    assert aws.scoring.score_job("Java Developer", desc, "Paris", sim)["score"] < 30


def test_us_only_is_flagged_and_below_threshold(aws):
    desc = LONG_DESC + " Must be located in the United States. US work authorization required."
    sim = aws.scoring.similarities([desc])[0]
    r = aws.scoring.score_job("DevOps Engineer", desc, "Remote", sim)
    assert "check-eligibility" in r["flags"]


def test_excluded_title(aws):
    sim = aws.scoring.similarities([LONG_DESC])[0]
    r = aws.scoring.score_job("DevOps Intern", LONG_DESC, "Paris", sim)
    assert "title-excluded" in r["flags"] and r["score"] < 60


def test_similarity_in_range(aws):
    sims = aws.scoring.similarities([LONG_DESC, "cooking pasta with tomatoes", ""])
    assert all(0 <= s <= 1 for s in sims) and sims[0] > sims[1]


# ---- ingest ----
def test_ingest_unauthorized(aws):
    assert aws.ingest.handler(event([job()], key="wrong"), None)["statusCode"] == 401


def test_ingest_store_then_duplicate(aws):
    first = json.loads(aws.ingest.handler(event([job()]), None)["body"])
    assert first["stored"] == 1 and first["scores"][0] >= 45
    second = json.loads(aws.ingest.handler(event([job()]), None)["body"])
    assert second["duplicates"] == 1 and second["stored"] == 0


def test_ingest_rejects_short_description(aws):
    body = json.loads(aws.ingest.handler(event([job(desc="too short")]), None)["body"])
    assert body["rejected"] == [0]


# ---- fetch ----
def test_fetch_stores_scored_jobs(aws, monkeypatch):
    def fake(url, timeout=15):
        return [{"legal": "notice"},
                {"id": 99, "position": "Senior DevOps Engineer", "company": "Remo", "location": "Worldwide",
                 "description": "<p>" + LONG_DESC + "</p>", "url": "https://remoteok.com/remote-jobs/99"},
                {"id": 100, "position": "Cloud Engineer", "company": "Other", "location": "",
                 "description": LONG_DESC, "url": "https://remoteok.com/remote-jobs/100"}]
    monkeypatch.setattr(aws.fetch, "http_get_json", fake)
    out = aws.fetch.handler({}, None)
    assert out["stored"] == 2 and out["errors"] == []
    out2 = aws.fetch.handler({}, None)
    assert out2["stored"] == 0 and out2["duplicates"] == 2


def test_fetch_failing_raises(aws, monkeypatch):
    def boom(url, timeout=15):
        raise OSError("down")
    monkeypatch.setattr(aws.fetch, "http_get_json", boom)
    import pytest
    with pytest.raises(RuntimeError):
        aws.fetch.handler({}, None)


# ---- digest ----
def test_digest_sends_once_and_clears(aws):
    aws.ingest.handler(event([job("1111111111"), job("2222222222", title="SRE")]), None)
    assert aws.digest.handler({}, None)["sent"] == 2
    assert aws.digest.handler({}, None)["sent"] == 0  # marked as notified, sparse index emptied
