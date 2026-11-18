#!/usr/bin/env python3
"""Tailor job-radar to YOUR CV in three commands. No AWS access and no network needed.

  init   draft infra/lambda/src/profile.json from your CV (skills, weights, titles, places, languages, level)
  check  validate a profile (a bad regex would crash all three Lambdas, so check before you deploy)
  try    score a real job posting against the profile and show where every point comes from

Typical run:
  python3 scripts/tailor.py init --cv cv.txt --titles "data engineer, analytics engineer" \\
      --locations "france, germany, netherlands" --languages "english, french" --level mid --force
  python3 scripts/tailor.py check
  python3 scripts/tailor.py try --title "Senior Data Engineer" --location "Remote, Europe" < posting.txt
  terraform -chdir=infra apply          # deploy it
"""
import argparse
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "infra" / "lambda" / "src"
DEFAULT_PROFILE = SRC / "profile.json"
sys.path.insert(0, str(SRC))

# ---------------------------------------------------------------------------------------------------------
# Skill vocabulary: (name shown in digests, regex). `init` counts how often each appears in your CV.
# Tech roles only: for another field, edit profile.json by hand (see the README).
# ---------------------------------------------------------------------------------------------------------
VOCAB = [
    ("python", r"\bpython\b"), ("java", r"\bjava\b(?!script)"), ("javascript", r"javascript|ecmascript"),
    ("typescript", r"typescript"), ("go", r"\bgolang\b|\bgo language\b"), ("rust", r"\brust\b"),
    ("c#", r"c#|csharp"), ("c++", r"c\+\+"), ("php", r"\bphp\b"), ("ruby", r"\bruby\b"), ("kotlin", r"\bkotlin\b"),
    ("swift", r"\bswift\b"), ("scala", r"\bscala\b"), ("sql", r"\bsql\b"), ("bash", r"\bbash\b|shell script"),
    ("react", r"\breact(\.?js)?\b"), ("angular", r"\bangular\b"), ("vue", r"\bvue(\.?js)?\b"), ("next.js", r"next\.?js"),
    ("node.js", r"node\.?js"), ("django", r"\bdjango\b"), ("flask", r"\bflask\b"), ("fastapi", r"\bfastapi\b"),
    ("spring", r"spring boot|\bspring\b"), (".net", r"\.net\b|dotnet"), ("rails", r"ruby on rails|\brails\b"),
    ("laravel", r"\blaravel\b"), ("graphql", r"\bgraphql\b"), ("rest api", r"rest(ful)? api"),
    ("postgresql", r"postgres(ql)?"), ("mysql", r"\bmysql\b"), ("mongodb", r"mongo(db)?"), ("redis", r"\bredis\b"),
    ("elasticsearch", r"elasticsearch|opensearch"), ("kafka", r"\bkafka\b"), ("spark", r"\bspark\b|pyspark"),
    ("airflow", r"\bairflow\b"), ("dbt", r"\bdbt\b"), ("snowflake", r"\bsnowflake\b"), ("bigquery", r"bigquery"),
    ("databricks", r"\bdatabricks\b"), ("pandas", r"\bpandas\b"), ("numpy", r"\bnumpy\b"),
    ("scikit-learn", r"scikit|sklearn"), ("tensorflow", r"\btensorflow\b"), ("pytorch", r"\bpytorch\b"),
    ("machine learning", r"machine learning|\bml\b"), ("nlp", r"\bnlp\b|natural language"),
    ("llm", r"\bllms?\b|large language model"), ("power bi", r"power ?bi"), ("tableau", r"\btableau\b"),
    ("etl", r"\betl\b|\belt\b|data pipelines?"), ("aws", r"\baws\b|amazon web services"), ("azure", r"\bazure\b"),
    ("gcp", r"\bgcp\b|google cloud"), ("terraform", r"terraform|opentofu"), ("kubernetes", r"kubernetes|k8s|\beks\b"),
    ("docker", r"\bdocker\b|containeri[sz]"), ("helm", r"\bhelm\b"), ("ansible", r"\bansible\b"),
    ("github actions", r"github actions"), ("gitlab ci", r"gitlab[- ]ci"), ("jenkins", r"\bjenkins\b"),
    ("argocd", r"argo ?cd|\bflux\b|gitops"), ("ci/cd", r"ci/cd|ci-cd|continuous (integration|delivery|deployment)"),
    ("prometheus", r"\bprometheus\b"), ("grafana", r"\bgrafana\b"), ("datadog", r"\bdatadog\b"),
    ("observability", r"observability|monitoring"), ("linux", r"\blinux\b"), ("lambda", r"\blambda\b|serverless"),
    ("infrastructure as code", r"infrastructure[- ]as[- ]code|\biac\b"), ("iam", r"\biam\b|least[- ]privilege|\boidc\b"),
    ("security", r"devsecops|\bsecurity\b|pentest|penetration test"), ("sre", r"\bsre\b|site reliability"),
    ("android", r"\bandroid\b"), ("ios", r"\bios\b"), ("flutter", r"\bflutter\b"), ("react native", r"react native"),
    ("git", r"\bgit\b"), ("agile", r"\bagile\b|\bscrum\b"), ("microservices", r"microservices?"),
    ("testing", r"pytest|junit|\bjest\b|unit tests?|test automation|selenium|cypress"),
    ("figma", r"\bfigma\b"), ("ux", r"\bux\b|user experience"),
]

# Excluded unless one of your target titles contains the key word (so a data scientist keeps "data scientist").
ROLE_EXCLUDES = [("designer", "designer"), ("frontend", "frontend"), ("front-end", "front"), ("ios", "ios"),
                 ("android", "android"), ("manager", "manager"), ("director", "director"), ("head of", "head"),
                 ("data scientist", "scientist")]
BASE_EXCLUDES = ["intern", "internship", "stage", "alternance", "apprenti", "apprentice", "sales",
                 "account executive", "recruiter"]
LANGUAGES = ["german", "dutch", "spanish", "italian", "portuguese", "polish", "swedish", "danish", "french",
             "english", "arabic", "chinese", "japanese", "korean", "russian", "turkish"]
LEVELS = {
    "junior": r"\b(senior|sr\.?|lead|principal|staff engineer|head of|director|vp)\b|\b([5-9]|1\d)\+? ?years",
    "mid": r"\b(principal|staff engineer|head of|director|vp)\b|\b(8|9|10|12|15)\+? ?years",
    "senior": r"\b(junior|jr\.?|entry[- ]level|graduate|trainee|intern(ship)?)\b",
}


def csv(text):
    return [t.strip().lower() for t in (text or "").split(",") if t.strip()]


def read_cv(path):
    path = Path(path)
    if not path.is_file():
        sys.exit(f"CV not found: {path}")
    if path.suffix.lower() == ".pdf":
        if shutil.which("pdftotext"):
            done = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True)
            if done.returncode != 0:
                sys.exit(f"pdftotext could not read {path}: {done.stderr.strip() or 'unknown error'}")
            return done.stdout
        try:
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        except ImportError:
            sys.exit("Cannot read PDFs here: install poppler (pdftotext) or `pip install pypdf`, or save your CV as .txt.")
    return path.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------------------------------------
def build_profile(cv_text, titles, locations, languages, level, excludes, base):
    """A draft profile dict. Contains NO text from the CV beyond skill names, so it is safe to commit."""
    counts = {}
    for name, pattern in VOCAB:
        n = len(re.findall(pattern, cv_text, re.IGNORECASE))
        if n:
            counts[name] = (n, pattern)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1][0])[:25]
    skills = [
        {"name": name, "weight": max(1, min(9, round(2 + 2.5 * math.log2(1 + n)))), "pattern": pattern}
        for name, (n, pattern) in ranked
    ]

    squashed = re.sub(r"[^a-z]", "", " ".join(titles))  # "front-end engineer" -> "frontendengineer"
    role_excludes = [term for term, key in ROLE_EXCLUDES if key not in squashed]  # fewer excludes is the safe direction
    title_exclude = list(dict.fromkeys(BASE_EXCLUDES + role_excludes + excludes))

    # Bag of words for the TF-IDF part: titles + skill names, repeated by weight. Deliberately not your CV text.
    profile_text = " ".join(titles + [(s["name"] + " ") * math.ceil(s["weight"] / 3) for s in skills]).strip()

    spoken = set(languages)
    blocked = [lang for lang in LANGUAGES if lang not in spoken]
    eligibility = [p for p in base["eligibility_patterns"] if "fluent" not in p]
    if blocked:
        eligibility.append(r"(fluent|native|proficien\w+)[^.\n]{0,40}(" + "|".join(blocked) + ")")

    profile = dict(base)
    profile.update({
        "profile_text": profile_text,
        "title_include": titles,
        "title_exclude": title_exclude,
        "skills": skills,
        "location_ok_pattern": (r"\b(" + "|".join(re.escape(loc) for loc in locations) + r")\b") if locations else "a^",
        "eligibility_patterns": eligibility,
        "seniority_penalty_pattern": LEVELS[level],
    })
    return profile, ranked


def cmd_init(args):
    out = Path(args.out)
    if out.exists() and not args.force:
        sys.exit(f"{out} already exists. Re-run with --force to replace it (git keeps your previous version), or pass --out FILE.")
    titles = csv(args.titles)
    if not titles:
        sys.exit('--titles is required, e.g. --titles "platform engineer, site reliability engineer"')
    languages = csv(args.languages) or ["english"]
    unknown = [lang for lang in languages if lang not in LANGUAGES]
    if unknown:
        print(f"note: unrecognised language(s) {unknown}; known: {', '.join(LANGUAGES)}")
    base = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    profile, ranked = build_profile(read_cv(args.cv), titles, csv(args.locations), languages, args.level, csv(args.exclude), base)
    if args.min_score:
        profile["digest_min_score"] = args.min_score
    if not profile["skills"]:
        print("warning: no known skills found in the CV. Is it a tech CV, and was the text extracted? Edit the skills by hand.")
    out.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {out}\n")
    print(f"Target titles : {', '.join(titles)}")
    print(f"Level         : {args.level}   (penalises: {LEVELS[args.level]})")
    print(f"Places        : {', '.join(csv(args.locations)) or 'none given: only remote jobs get the location bonus'}")
    print(f"Languages     : {', '.join(languages)}   (other languages flagged when 'fluent' is required)")
    print("Skills (weight = how prominent in your CV, 1-9):")
    for s, (name, (n, _)) in zip(profile["skills"], ranked):
        print(f"  {s['weight']}  {name:<24} {n} mention(s)")
    print("\nNext:  python3 scripts/tailor.py check   then   python3 scripts/tailor.py try --title '...' < posting.txt")
    return 0


# ---------------------------------------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------------------------------------
def check_profile(p):
    """(errors, warnings, info) for a profile dict. Errors would break the Lambdas or the scoring."""
    errors, warnings, info = [], [], []

    def need(key, typ):
        if key not in p:
            errors.append(f"missing key '{key}'")
        elif not isinstance(p[key], typ) or isinstance(p[key], bool):
            errors.append(f"'{key}' must be {typ.__name__}")
        else:
            return True
        return False

    for key in ("digest_min_score", "store_min_score", "max_digest_items"):
        need(key, int)
    for key in ("profile_text", "remote_pattern", "location_ok_pattern", "seniority_penalty_pattern"):
        need(key, str)
    for key in ("title_include", "title_exclude", "skills", "eligibility_patterns"):
        need(key, list)
    if errors:
        return errors, warnings, info

    def regex(label, pattern):
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as err:
            errors.append(f"{label}: invalid regex ({err})")
            return
        if rx.search(""):
            errors.append(f"{label}: matches the empty string, so it would match EVERY job: {pattern!r}")

    for key in ("remote_pattern", "location_ok_pattern", "seniority_penalty_pattern"):
        if key != "location_ok_pattern":  # "a^" (never matches) is the valid way to switch the location bonus off
            regex(key, p[key])
        else:
            try:
                re.compile(p[key], re.IGNORECASE)
            except re.error as err:
                errors.append(f"{key}: invalid regex ({err})")
    for i, pattern in enumerate(p["eligibility_patterns"]):
        regex(f"eligibility_patterns[{i}]", pattern)

    for key in ("title_include", "title_exclude"):
        if not all(isinstance(t, str) and t.strip() for t in p[key]):
            errors.append(f"{key} must be a list of non-empty strings")
    if not p["title_include"]:
        errors.append("title_include is empty: no job could ever earn the +30 title bonus")

    names, total = [], 0
    for i, skill in enumerate(p["skills"]):
        if not (isinstance(skill, dict) and isinstance(skill.get("name"), str) and isinstance(skill.get("pattern"), str)
                and isinstance(skill.get("weight"), int) and not isinstance(skill.get("weight"), bool)):
            errors.append(f"skills[{i}] needs a text 'name', a text 'pattern' and an integer 'weight'")
            continue
        regex(f"skills[{i}] ({skill['name']})", skill["pattern"])
        if skill["weight"] < 1:
            errors.append(f"skills[{i}] ({skill['name']}): weight must be at least 1")
        elif skill["weight"] > 9:
            warnings.append(f"skills[{i}] ({skill['name']}): weight {skill['weight']} is above 9; a few of these alone fill the 40-point cap")
        names.append(skill["name"].lower())
        total += skill["weight"]
    if not p["skills"]:
        errors.append("skills is empty: skills are worth up to 40 of the 100 points")
    if errors:
        return errors, warnings, info

    for dup in sorted({n for n in names if names.count(n) > 1}):
        warnings.append(f"skill '{dup}' is listed more than once")
    if total < 40:
        warnings.append(f"skill weights add up to {total}: even a posting naming all of them cannot reach the 40-point cap")
    if len(p["profile_text"]) < 40:
        warnings.append("profile_text is very short; the TF-IDF similarity part will be weak (aim for titles and skills, 100+ characters)")
    overlap = sorted(set(map(str.lower, p["title_include"])) & set(map(str.lower, p["title_exclude"])))
    if overlap:
        warnings.append(f"in both title_include and title_exclude (exclude wins): {overlap}")
    if not 0 <= p["store_min_score"] <= p["digest_min_score"] <= 100:
        warnings.append("expected 0 <= store_min_score <= digest_min_score <= 100")
    info.append(f"{len(p['skills'])} skills (total weight {total}, cap 40), {len(p['title_include'])} target titles, "
                f"digest threshold {p['digest_min_score']}, store threshold {p['store_min_score']}")
    return errors, warnings, info


def cmd_check(args):
    try:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(f"ERROR: cannot read {args.profile}: {err}")
        return 1
    errors, warnings, info = check_profile(profile)
    for line in errors:
        print(f"ERROR   {line}")
    for line in warnings:
        print(f"warning {line}")
    for line in info:
        print(f"ok      {line}")
    print("\nInvalid: fix the errors before deploying." if errors else "\nValid. Deploy with: terraform -chdir=infra apply")
    return 1 if errors else 0


# ---------------------------------------------------------------------------------------------------------
# try
# ---------------------------------------------------------------------------------------------------------
def score_with(profile_path, title, location, description, remote=False):
    """Score with a given profile file (scoring reads it at import, so point it there and reload)."""
    previous = os.environ.get("JOB_RADAR_PROFILE")
    os.environ["JOB_RADAR_PROFILE"] = str(profile_path)
    try:
        import scoring
        scoring = importlib.reload(scoring)
        sim = scoring.similarities([description])[0]
        return scoring.score_job(title, description, location, sim, remote_hint=remote), sim, scoring.PROFILE
    finally:
        if previous is None:
            os.environ.pop("JOB_RADAR_PROFILE", None)
        else:
            os.environ["JOB_RADAR_PROFILE"] = previous
        import scoring
        importlib.reload(scoring)


def cmd_try(args):
    description = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not description.strip():
        sys.exit("Give the posting text on stdin (python3 scripts/tailor.py try --title '...' < posting.txt) or with --file.")
    result, sim, profile = score_with(args.profile, args.title, args.location, description, args.remote)
    verdict = "FIT, would be emailed" if result["score"] >= profile["digest_min_score"] else \
        ("stored, not emailed" if result["score"] >= profile["store_min_score"] else "dropped (below the store threshold)")
    print(f"Score {result['score']}/100   ({verdict}; fit threshold {profile['digest_min_score']})\n")
    notes = {
        "title": "target title (+30) or excluded title (-30)",
        "skills": "matched: " + (", ".join(result["matched"]) or "none") + "  (capped at 40)",
        "similarity": f"TF-IDF cosine {sim:.2f} (0.25 = full 10 points)",
        "remote": "remote wording found",
        "location": "remote, or a place from your list",
        "eligibility": "visa / residency / language wording",
        "seniority": "level wording that does not match your --level",
    }
    for part, points in result["parts"].items():
        print(f"  {part:<12}{points:>+4}   {notes[part] if points or part in ('skills',) else ''}")
    if result["flags"]:
        print(f"\nFlags: {', '.join(result['flags'])}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="draft profile.json from a CV")
    i.add_argument("--cv", required=True, help="your CV as .txt, .md or .pdf")
    i.add_argument("--titles", required=True, help='target job titles, comma separated: "platform engineer, sre"')
    i.add_argument("--locations", default="", help='places you would work, comma separated: "france, germany"')
    i.add_argument("--languages", default="english", help='languages you speak: "english, french"')
    i.add_argument("--level", choices=sorted(LEVELS), default="mid", help="penalise postings above (junior) / below (senior) your level")
    i.add_argument("--exclude", default="", help="extra title words to rule out: \"consultant, support\"")
    i.add_argument("--min-score", type=int, default=0, help="digest threshold (default keeps the current value)")
    i.add_argument("--out", default=str(DEFAULT_PROFILE))
    i.add_argument("--force", action="store_true", help="overwrite an existing file")
    i.set_defaults(fn=cmd_init)

    c = sub.add_parser("check", help="validate a profile")
    c.add_argument("--profile", default=str(DEFAULT_PROFILE))
    c.set_defaults(fn=cmd_check)

    t = sub.add_parser("try", help="score one posting and show the breakdown")
    t.add_argument("--profile", default=str(DEFAULT_PROFILE))
    t.add_argument("--title", required=True)
    t.add_argument("--location", default="")
    t.add_argument("--remote", action="store_true", help="treat the posting as remote")
    t.add_argument("--file", help="posting text file (default: stdin)")
    t.set_defaults(fn=cmd_try)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
