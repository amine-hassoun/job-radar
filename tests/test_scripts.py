import os
import sys

from conftest import LONG_DESC

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


def put(aws, source, ext_id, title, desc=LONG_DESC, similarity=0.2):
    item = aws.store.build_item(source=source, ext_id=ext_id, title=title, company="Co", location="Remote",
                                description=desc, url=f"https://x/{ext_id}", similarity=similarity,
                                captured=(source == "linkedin-extension"))
    assert aws.store.put_new(item)


def test_export_remoteok_filters_sorts_and_formats(aws):
    import export_remoteok
    put(aws, "remoteok", "11111", "DevOps Engineer")
    put(aws, "remoteok", "22222", "Java Developer", desc="Spring and Oracle for banks. " * 5)  # low score
    put(aws, "linkedin-extension", "33333", "SRE")                                              # other source
    jobs = export_remoteok.fetch(aws.store.TABLE, "remoteok", 45, "2000-01-01", 15)
    assert [j["job_id"] for j in jobs] == ["remoteok#11111"]
    text = export_remoteok.to_prompt(jobs)
    assert "## 1. DevOps Engineer — Co" in text and "We are hiring a DevOps engineer" in text and "URL: https://x/11111" in text
    assert [j["job_id"] for j in export_remoteok.fetch(aws.store.TABLE, "all", 45, "2000-01-01", 15)].count("linkedin-extension#33333") == 1
    assert export_remoteok.fetch(aws.store.TABLE, "remoteok", 45, "2999-01-01", 15) == []  # --days cutoff


def test_reset_wipes_every_item(aws):
    import reset_data
    put(aws, "remoteok", "11111", "DevOps Engineer")
    put(aws, "linkedin-extension", "33333", "SRE")
    keys = reset_data.all_keys(aws.store.TABLE)
    assert sorted(keys) == ["linkedin-extension#33333", "remoteok#11111"]
    assert reset_data.wipe(aws.store.TABLE, keys) == 2
    assert reset_data.all_keys(aws.store.TABLE) == []
