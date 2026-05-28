"""
The fit engine. Sends each surviving job + the profile to the LLM and gets back
a structured fit score. This is the part that replaces what a human recruiter
(or me, manually) does when sorting roles by fit.

Set ANTHROPIC_API_KEY in your environment to enable real ranking.
With no key, run() returns the jobs unranked (dry run) so the rest of the
pipeline still works end to end while you build.

Cost note: ranking is the only paid step. The prefilter already cut the list
down, but for big runs you can batch multiple jobs into one prompt to save tokens.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from prompts import FIT_RANKING, PROFILE_SCHEMA  # noqa: F401

MODEL = "claude-sonnet-4-6"  # swap as needed


def _rank_one(client, profile_json, job):
    prompt = FIT_RANKING.format(
        profile_json=profile_json,
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        description=job.get("description", "") or "(no description available)",
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"score": None, "tier": "unknown", "reasons": ["could not parse model output"],
                "hard_disqualifiers": [], "matched_skills": [], "missing_skills": []}


def run(jobs, profile):
    """Attach a 'fit' dict to each job. Sorts strong -> possible -> skip by score."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("  [dry run] no ANTHROPIC_API_KEY set - skipping LLM ranking.")
        for j in jobs:
            j["fit"] = None
        return jobs

    import anthropic
    client = anthropic.Anthropic(api_key=key)
    profile_json = json.dumps(profile, indent=2)

    print(f"  ranking {len(jobs)} jobs in parallel (10 workers)...")
    done = [0]
    def _work(j):
        j["fit"] = _rank_one(client, profile_json, j)
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"    {done[0]}/{len(jobs)} ranked")
        return j
    with ThreadPoolExecutor(max_workers=30) as ex:
        list(ex.map(_work, jobs))

    tier_rank = {"strong": 0, "possible": 1, "skip": 2, "unknown": 3}
    jobs.sort(key=lambda j: (
        tier_rank.get((j["fit"] or {}).get("tier", "unknown"), 3),
        -((j["fit"] or {}).get("score") or 0),
    ))
    return jobs
