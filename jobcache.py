"""
jobcache.py — remember what we've seen and ranked, so re-runs are nearly free.

Persisted in ranked_cache.json:
  - seen:   {job_id: first_seen_date}   -> lets us flag brand-new postings
  - ranked: {job_id: fit_dict}          -> lets us skip PAYING to re-rank

A job_id is a stable hash of company|title|location, so the same posting keeps
the same id across runs even as the source list shuffles. This is what turns a
$1 run into a ~$0.05 run the next day: only genuinely new jobs hit the LLM.
"""
import datetime
import hashlib
import json
import os

CACHE_FILE = os.environ.get("CACHE_FILE", "ranked_cache.json")


def job_id(job):
    raw = f"{job.get('company','')}|{job.get('title','')}|{job.get('location','')}".lower()
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load():
    if not os.path.exists(CACHE_FILE):
        return {"seen": {}, "ranked": {}}
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        data.setdefault("seen", {})
        data.setdefault("ranked", {})
        return data
    except Exception:
        return {"seen": {}, "ranked": {}}


def save(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def today():
    return datetime.date.today().isoformat()
