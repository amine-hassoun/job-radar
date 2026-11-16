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


# ---- extension API: list / export / delete ----
def call(aws, body):
    out = aws.ingest.handler({"headers": {"x-job-radar-key": "s3cret"}, "body": json.dumps(body)}, None)
    return out["statusCode"], json.loads(out["body"])


def test_list_only_fits_and_order(aws):
    aws.ingest.handler(event([job("1111111111", title="DevOps Engineer"),
                              job("2222222222", title="Java Developer",
                                  desc="Spring microservices for banking, Oracle and Maven. " * 4)]), None)
    status, body = call(aws, {"action": "list"})
    assert status == 200 and [j["jobId"] for j in body["jobs"]][0] == "1111111111" and len(body["jobs"]) == 2
    status, body = call(aws, {"action": "list", "only_fits": True})
    assert [j["jobId"] for j in body["jobs"]] == ["1111111111"] and body["min_score"] == 45


def test_remoteok_jobs_are_not_in_captured_list(aws, monkeypatch):
    monkeypatch.setattr(aws.fetch, "http_get_json", lambda url, timeout=15: [
        {"id": 7, "position": "DevOps Engineer", "company": "R", "description": LONG_DESC, "url": "https://remoteok.com/7"}])
    aws.fetch.handler({}, None)
    assert call(aws, {"action": "list"})[1]["jobs"] == []


def test_export_returns_full_description_and_not_other_sources(aws):
    aws.ingest.handler(event([job("1111111111")]), None)
    status, body = call(aws, {"action": "export", "job_ids": ["1111111111", "5555555555"]})
    assert status == 200 and len(body["jobs"]) == 1
    assert body["jobs"][0]["description"].startswith("We are hiring") and body["jobs"][0]["score"] >= 45


def test_delete_removes_from_list_and_table(aws):
    aws.ingest.handler(event([job("1111111111"), job("2222222222")]), None)
    assert call(aws, {"action": "delete", "job_ids": ["1111111111"]}) == (200, {"deleted": 1})
    assert [j["jobId"] for j in call(aws, {"action": "list"})[1]["jobs"]] == ["2222222222"]
    assert call(aws, {"action": "export", "job_ids": ["1111111111"]})[1]["jobs"] == []


def test_delete_cannot_touch_other_sources(aws):
    item = aws.store.build_item(source="remoteok", ext_id="1111111111", title="DevOps", company="R",
                                location="", description=LONG_DESC, url="u", similarity=0.2)
    aws.store.put_new(item)
    call(aws, {"action": "delete", "job_ids": ["1111111111"]})
    assert "Item" in aws.store.TABLE.get_item(Key={"job_id": "remoteok#1111111111"})


def test_api_validation(aws):
    assert call(aws, {"action": "nope"})[0] == 400
    assert call(aws, {"action": "delete", "job_ids": []})[0] == 400
    assert call(aws, {"action": "delete", "job_ids": ["abc"]})[0] == 400
    assert call(aws, {"action": "export"})[0] == 400
    assert aws.ingest.handler({"headers": {"x-job-radar-key": "bad"}, "body": json.dumps({"action": "list"})}, None)["statusCode"] == 401


# ---- RemoteOK tab: same API, source = remoteok ----
def remoteok_feed(monkeypatch, aws, ids=(1000007, 1000008)):
    jobs = [{"id": i, "position": "DevOps Engineer", "company": f"R{i}", "location": "Worldwide",
             "description": LONG_DESC, "url": f"https://remoteok.com/remote-jobs/{i}"} for i in ids]
    monkeypatch.setattr(aws.fetch, "http_get_json", lambda url, timeout=15: [{"legal": "notice"}] + jobs)


def test_remoteok_list_export_and_isolation(aws, monkeypatch):
    remoteok_feed(monkeypatch, aws)
    aws.fetch.handler({}, None)
    aws.ingest.handler(event([job("1111111111")]), None)
    status, body = call(aws, {"action": "list", "source": "remoteok", "only_fits": True})
    assert status == 200 and body["source"] == "remoteok" and sorted(j["jobId"] for j in body["jobs"]) == ["1000007", "1000008"]
    assert [j["jobId"] for j in call(aws, {"action": "list"})[1]["jobs"]] == ["1111111111"]  # LinkedIn list unchanged
    exported = call(aws, {"action": "export", "source": "remoteok", "job_ids": ["1000007"]})[1]["jobs"]
    assert len(exported) == 1 and exported[0]["description"].startswith("We are hiring")
    assert call(aws, {"action": "export", "job_ids": ["1000007"]})[1]["jobs"] == []  # default source is LinkedIn


def test_remoteok_delete_leaves_tombstone_and_blocks_refetch(aws, monkeypatch):
    remoteok_feed(monkeypatch, aws)
    aws.fetch.handler({}, None)
    assert call(aws, {"action": "delete", "source": "remoteok", "job_ids": ["1000007"]}) == (200, {"deleted": 1})
    assert [j["jobId"] for j in call(aws, {"action": "list", "source": "remoteok"})[1]["jobs"]] == ["1000008"]
    item = aws.store.TABLE.get_item(Key={"job_id": "remoteok#1000007"})["Item"]
    assert "applied_at" in item and "description" not in item and "captured_pk" not in item and "digest_pk" not in item
    out = aws.fetch.handler({}, None)
    assert out["stored"] == 0 and out["duplicates"] == 2          # the deleted listing does not come back
    assert [j["jobId"] for j in call(aws, {"action": "list", "source": "remoteok"})[1]["jobs"]] == ["1000008"]


def test_remoteok_delete_unknown_id_creates_nothing(aws):
    call(aws, {"action": "delete", "source": "remoteok", "job_ids": ["99999"]})
    assert "Item" not in aws.store.TABLE.get_item(Key={"job_id": "remoteok#99999"})


def test_unknown_source_rejected(aws):
    for action in ("list", "export", "delete"):
        assert call(aws, {"action": action, "source": "adzuna", "job_ids": ["12345"]})[0] == 400
