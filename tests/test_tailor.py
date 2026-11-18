import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import tailor  # noqa: E402  (scripts/tailor.py)

CV = """Jane Doe - jane.doe@example.com - +33 6 11 22 33 44
Data Engineer. Built ETL data pipelines with Airflow, Spark and dbt on Snowflake and AWS.
Python (pandas), SQL, PostgreSQL, Kafka. Airflow DAGs, Airflow scheduling. Docker, Terraform, Git.
"""
DATA_JOB = ("We are hiring a data engineer to build ETL pipelines with Airflow, Spark and dbt on Snowflake and AWS. "
            "Python and SQL required. Remote across Europe.")
DEVOPS_JOB = "Platform engineer: Terraform, Kubernetes, GitHub Actions and AWS. Remote across Europe."


@pytest.fixture
def made(tmp_path, capsys):
    """Generate a profile for the sample CV and return its path."""
    def make(titles="data engineer, analytics engineer", locations="france, germany", languages="english, french",
             level="mid", extra=()):
        cv = tmp_path / "cv.txt"; cv.write_text(CV)
        out = tmp_path / "profile.json"
        assert tailor.main(["init", "--cv", str(cv), "--titles", titles, "--locations", locations,
                            "--languages", languages, "--level", level, "--out", str(out), "--force", *extra]) == 0
        capsys.readouterr()
        return out
    return make


def load(path):
    return json.loads(Path(path).read_text())


def test_bundled_profile_is_valid():
    errors, _, _ = tailor.check_profile(load(tailor.DEFAULT_PROFILE))
    assert errors == []


def test_init_extracts_weighted_skills_from_the_cv(made):
    p = load(made())
    weights = {s["name"]: s["weight"] for s in p["skills"]}
    assert {"python", "sql", "airflow", "spark", "dbt", "snowflake", "aws", "terraform"} <= set(weights)
    assert weights["airflow"] > weights["python"]            # mentioned three times vs once
    assert "kubernetes" not in weights                       # not in the CV
    assert p["title_include"] == ["data engineer", "analytics engineer"]


def test_init_never_copies_personal_data_from_the_cv(made):
    text = made().read_text().lower()
    for private in ("jane", "doe", "example.com", "+33", "11 22"):
        assert private not in text


def test_generated_profile_passes_check(made):
    errors, warnings, _ = tailor.check_profile(load(made()))
    assert errors == [] and warnings == []


def test_locations_and_languages_become_patterns(made):
    p = load(made())
    loc = re.compile(p["location_ok_pattern"], re.I)
    assert loc.search("Paris, France") and loc.search("Berlin, Germany") and not loc.search("Austin, Texas")
    lang = [re.compile(x, re.I) for x in p["eligibility_patterns"]]
    flagged = lambda text: any(rx.search(text) for rx in lang)  # noqa: E731
    assert flagged("Fluent German is required") and flagged("native Dutch speaker")
    assert not flagged("Fluent French and English") and flagged("US work authorization required")  # speaks fr/en; US rule kept


def test_no_locations_switches_the_location_bonus_off_and_still_validates(made):
    p = load(made(locations=""))
    assert p["location_ok_pattern"] == "a^" and tailor.check_profile(p)[0] == []


def test_role_excludes_follow_your_target_titles(made):
    ds = load(made(titles="data scientist"))["title_exclude"]
    assert "data scientist" not in ds and "designer" in ds and "frontend" in ds
    fe = load(made(titles="front-end engineer"))["title_exclude"]
    assert "frontend" not in fe and "front-end" not in fe and "ios" in fe
    assert "manager" not in load(made(titles="engineering manager"))["title_exclude"]
    assert "consultant" in load(made(extra=["--exclude", "consultant"]))["title_exclude"]


def test_tailoring_changes_the_scores(made):
    mine = made()
    data_mine, _, _ = tailor.score_with(mine, "Data Engineer", "Remote, Europe", DATA_JOB)
    data_default, _, _ = tailor.score_with(tailor.DEFAULT_PROFILE, "Data Engineer", "Remote, Europe", DATA_JOB)
    devops_mine, _, _ = tailor.score_with(mine, "Platform Engineer", "Remote, Europe", DEVOPS_JOB)
    assert data_mine["score"] >= 45 > data_default["score"]       # a data engineer's profile fits the data job; the DevOps one doesn't
    assert devops_mine["score"] < 45                              # and the DevOps job no longer fits
    assert "airflow" in data_mine["matched"]


def test_try_breakdown_adds_up_and_restores_the_default_profile(made):
    result, sim, profile = tailor.score_with(made(), "Data Engineer", "Paris, France", DATA_JOB)
    assert result["score"] == max(0, min(100, sum(result["parts"].values())))
    assert 0 <= sim <= 1 and profile["title_include"][0] == "data engineer"
    assert "JOB_RADAR_PROFILE" not in os.environ
    import scoring
    assert "kubernetes" in {s["name"] for s in scoring.PROFILE["skills"]}   # back to the bundled DevOps profile


@pytest.mark.parametrize("level,wording,flagged", [
    ("senior", "Junior data engineer, graduate welcome", True), ("senior", "Data engineer, 5 years", False),
    ("junior", "Senior data engineer, 8+ years", True), ("junior", "Data engineer, graduate welcome", False),
    ("mid", "Principal data engineer", True), ("mid", "Senior data engineer, 5 years", False),
])
def test_level_changes_which_wording_is_penalised(made, level, wording, flagged):
    result, _, _ = tailor.score_with(made(level=level), "Data Engineer", "Remote", wording + ". " + DATA_JOB)
    assert ("senior-wording" in result["flags"]) is flagged


def test_check_catches_what_would_break_the_lambdas():
    good = load(tailor.DEFAULT_PROFILE)

    def errors_of(**changes):
        return tailor.check_profile({**good, **changes})[0]

    assert any("invalid regex" in e for e in errors_of(remote_pattern="(unclosed"))
    assert any("empty string" in e for e in errors_of(seniority_penalty_pattern="a*"))
    assert any("empty string" in e for e in errors_of(eligibility_patterns=["x|"]))
    assert any("title_include is empty" in e for e in errors_of(title_include=[]))
    assert any("skills is empty" in e for e in errors_of(skills=[]))
    assert any("weight" in e for e in errors_of(skills=[{"name": "x", "pattern": "x", "weight": 0}]))
    assert any("skills[0]" in e for e in errors_of(skills=[{"name": "x", "pattern": "x", "weight": "9"}]))
    assert any("must be int" in e for e in errors_of(digest_min_score="45"))
    assert tailor.check_profile({k: v for k, v in good.items() if k != "skills"})[0] == ["missing key 'skills'"]
    assert errors_of() == []


def test_check_warns_about_weak_profiles():
    good = load(tailor.DEFAULT_PROFILE)
    warnings = tailor.check_profile({**good, "skills": [{"name": "x", "pattern": r"\bx\b", "weight": 3}],
                                     "profile_text": "short", "store_min_score": 60})[1]
    text = " ".join(warnings)
    assert "cannot reach the 40-point cap" in text and "profile_text is very short" in text and "store_min_score" in text


def test_init_refuses_to_overwrite_without_force(tmp_path):
    cv = tmp_path / "cv.txt"; cv.write_text(CV)
    out = tmp_path / "p.json"; out.write_text("{}")
    with pytest.raises(SystemExit) as err:
        tailor.main(["init", "--cv", str(cv), "--titles", "data engineer", "--out", str(out)])
    assert "--force" in str(err.value) and out.read_text() == "{}"


def test_init_requires_titles(tmp_path):
    cv = tmp_path / "cv.txt"; cv.write_text(CV)
    with pytest.raises(SystemExit) as err:
        tailor.main(["init", "--cv", str(cv), "--titles", " , ", "--out", str(tmp_path / "p.json")])
    assert "--titles is required" in str(err.value)


def test_runs_as_a_script():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "tailor.py"), "check"], capture_output=True, text=True)
    assert r.returncode == 0 and "Valid" in r.stdout
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "tailor.py"), "try", "--title", "SRE"],
                       input="Terraform AWS Kubernetes remote", capture_output=True, text=True)
    assert r.returncode == 0 and "Score" in r.stdout


def test_missing_or_unreadable_cv_gives_a_clear_message(tmp_path):
    with pytest.raises(SystemExit) as err:
        tailor.main(["init", "--cv", str(tmp_path / "nope.pdf"), "--titles", "sre", "--out", str(tmp_path / "p.json")])
    assert "CV not found" in str(err.value)
    broken = tmp_path / "broken.pdf"; broken.write_text("this is not a pdf")
    with pytest.raises(SystemExit) as err:
        tailor.main(["init", "--cv", str(broken), "--titles", "sre", "--out", str(tmp_path / "p.json")])
    assert "could not read" in str(err.value) or "Cannot read PDFs" in str(err.value)
