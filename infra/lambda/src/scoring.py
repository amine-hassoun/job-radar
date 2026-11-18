"""Fit scoring: transparent rules + TF-IDF cosine. No network, no LLM, no extra dependencies."""
import json
import math
import os
import re
from collections import Counter

# JOB_RADAR_PROFILE lets scripts/profile.py try a profile before it is deployed. The Lambdas never set it.
_PROFILE_PATH = os.environ.get("JOB_RADAR_PROFILE") or os.path.join(os.path.dirname(__file__), "profile.json")
with open(_PROFILE_PATH, encoding="utf-8") as f:
    PROFILE = json.load(f)

FLAGS = re.IGNORECASE

_TITLE_INCLUDE = re.compile(r"\b(" + "|".join(map(re.escape, PROFILE["title_include"])) + r")", FLAGS)
_TITLE_EXCLUDE = re.compile(r"\b(" + "|".join(map(re.escape, PROFILE["title_exclude"])) + r")\b", FLAGS)
_SKILLS = [(s["name"], s["weight"], re.compile(s["pattern"], FLAGS)) for s in PROFILE["skills"]]
_REMOTE = re.compile(PROFILE["remote_pattern"], FLAGS)
_LOCATION_OK = re.compile(PROFILE["location_ok_pattern"], FLAGS)
_ELIGIBILITY = [re.compile(p, FLAGS) for p in PROFILE["eligibility_patterns"]]
_SENIORITY = re.compile(PROFILE["seniority_penalty_pattern"], FLAGS)

SKILL_CAP = 40
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or our that the their this to we will with you your "
    "de la le les des du un une et en pour sur avec dans nous vous ou au aux est sont "
    "experience team work working role job years year ability strong skills knowledge etc".split()
)
_TOKEN = re.compile(r"[a-z][a-z0-9+#]{1,}")


def _tokens(text):
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


def _vector(tokens, idf):
    # Sublinear TF (1 + ln count) damps keyword stuffing; multiplied by IDF.
    return {t: (1 + math.log(c)) * idf[t] for t, c in Counter(tokens).items()}


def _cosine(a, b):
    dot = sum(w * b[t] for t, w in a.items() if t in b)
    norm = math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))
    return dot / norm if norm else 0.0


def similarities(docs):
    """Cosine similarity (0-1) of each doc against the profile. IDF is computed over profile + docs."""
    corpus = [_tokens(PROFILE["profile_text"])] + [_tokens(d) for d in docs]
    n = len(corpus)
    df = Counter()
    for toks in corpus:
        df.update(set(toks))
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}  # smoothed IDF, always > 0
    vectors = [_vector(toks, idf) for toks in corpus]
    return [_cosine(vectors[0], v) for v in vectors[1:]]


def score_job(title, description, location, similarity, remote_hint=False):
    """Return {"score": 0-100 int, "matched": [skills], "flags": [strings], "parts": {component: points}}."""
    title = title or ""
    description = description or ""
    location = location or ""
    text = f"{title}\n{description}"
    flags = []
    parts = {"title": 0, "skills": 0, "similarity": 0, "remote": 0, "location": 0, "eligibility": 0, "seniority": 0}

    if _TITLE_EXCLUDE.search(title):
        parts["title"] = -30
        flags.append("title-excluded")
    elif _TITLE_INCLUDE.search(title):
        parts["title"] = 30

    matched = [(name, weight) for name, weight, rx in _SKILLS if rx.search(text)]
    parts["skills"] = min(SKILL_CAP, sum(w for _, w in matched))

    parts["similarity"] = round(10 * min(1.0, similarity / 0.25))  # sim of 0.25+ already counts as a strong match

    is_remote = remote_hint or bool(_REMOTE.search(f"{title} {location} {description[:1500]}"))
    if is_remote:
        parts["remote"] = 10
    if is_remote or _LOCATION_OK.search(location):
        parts["location"] = 5

    if any(rx.search(text) for rx in _ELIGIBILITY):
        parts["eligibility"] = -30
        flags.append("check-eligibility")  # visa / residency / language wording: read the posting
    if _SENIORITY.search(text):
        parts["seniority"] = -10
        flags.append("senior-wording")

    matched.sort(key=lambda m: -m[1])
    return {
        "score": max(0, min(100, int(sum(parts.values())))),
        "matched": [name for name, _ in matched[:12]],
        "flags": flags,
        "parts": parts,
    }
