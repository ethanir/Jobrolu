"""
Orchestrator — the FUNNEL version (cost-correct).

  profile -> pull jobs -> discover companies -> prefilter (free)
          -> heuristic score ALL (free) -> LLM-rank only TOP_N (cheap)
          -> heuristic-tier the rest (free) -> ranked feed

Cost: a full run is ~$1 instead of ~$70, because we only pay the LLM for the
top TOP_N most promising jobs. Set TOP_N=0 for a totally free run (no LLM).

Run:
    python main.py my_profile.json
    TOP_N=0  python main.py my_profile.json     # free, no API cost
    TOP_N=200 python main.py my_profile.json     # rank more (costs more)
"""
import csv
import datetime
import json
import os
import sys

import sources
import registry
import prefilter
import score
import rank
import enrich
import jobcache

TOP_N = int(os.environ.get("TOP_N", "100"))   # how many jobs the LLM ranks

SEED_COMPANIES = [
    {"name": "Stripe",     "ats": "greenhouse", "token": "stripe"},
    {"name": "Databricks", "ats": "greenhouse", "token": "databricks"},
    {"name": "Ramp",       "ats": "ashby",      "token": "ramp"},
    {"name": "Notion",     "ats": "ashby",      "token": "notion"},
]


def load_profile(path):
    with open(path) as f:
        return json.load(f)


def enrich_contacts(jobs, limit=15):
    for j in jobs:
        j["contacts"] = []
    if not os.environ.get("APOLLO_API_KEY"):
        return jobs
    strong = [j for j in jobs if (j.get("fit") or {}).get("tier") == "strong"][:limit]
    print(f"  enriching {len(strong)} strong-tier jobs...")
    for j in strong:
        j["contacts"] = enrich.find_contacts(j)
    return jobs


def fmt_date(ts):
    return datetime.date.fromtimestamp(ts).isoformat() if ts else ""


def main():
    profile_path = sys.argv[1] if len(sys.argv) > 1 else "profile.example.json"
    profile = load_profile(profile_path)
    print(f"Profile: {profile.get('name')} | targets: {', '.join(profile.get('target_titles', []))}\n")

    reg = registry.load()
    companies = {f"{c['ats']}:{c['token']}": c for c in SEED_COMPANIES}
    companies.update(reg)
    company_list = list(companies.values())
    print(f"Pulling jobs from {len(company_list)} companies + curated repo...")
    jobs = sources.pull_all(company_list, include_repo=True)
    print(f"  total pulled: {len(jobs)}\n")

    reg, n_new = registry.discover_from_jobs(jobs, reg)
    registry.save(reg)
    print(f"Registry: +{n_new} new -> {len(reg)} total known\n")

    print("Prefiltering...")
    jobs = prefilter.prefilter(jobs, profile)
    print(f"  survived prefilter: {len(jobs)}\n")

    print("Free heuristic scoring (no API cost)...")
    jobs = score.rank_free(jobs, profile)
    print(f"  scored {len(jobs)} jobs; top free score: {jobs[0]['_score'] if jobs else 0}\n")

    # Cache: flag brand-new postings + reuse prior rankings so re-runs are cheap.
    cache = jobcache.load()
    n_new_postings = 0
    for j in jobs:
        j["_id"] = jobcache.job_id(j)
        j["is_new"] = j["_id"] not in cache["seen"]
        if j["is_new"]:
            n_new_postings += 1
    print(f"  {n_new_postings} brand-new postings since last run\n")

    top = jobs[:TOP_N]
    rest = jobs[TOP_N:]

    if TOP_N > 0:
        need_llm = [j for j in top if j["_id"] not in cache["ranked"]]
        cached = [j for j in top if j["_id"] in cache["ranked"]]
        print(f"LLM-ranking {len(need_llm)} new jobs "
              f"({len(cached)} loaded free from cache)...")
        if need_llm:
            rank.run(need_llm, profile)               # the only paid step
            for j in need_llm:
                cache["ranked"][j["_id"]] = j.get("fit") or score.heuristic_fit(j)
        for j in cached:
            j["fit"] = cache["ranked"][j["_id"]]
    else:
        print("TOP_N=0 -> skipping LLM entirely (free run).\n")
        for j in top:
            j["fit"] = score.heuristic_fit(j)

    for j in rest:                        # everything else: free heuristic tier
        j["fit"] = score.heuristic_fit(j)

    # Remember everything we saw this run (so next run knows what's new).
    for j in jobs:
        cache["seen"].setdefault(j["_id"], jobcache.today())
    jobcache.save(cache)

    all_jobs = top + rest
    tier_rank = {"strong": 0, "possible": 1, "skip": 2, "unknown": 3}
    all_jobs.sort(key=lambda j: (
        not j.get("is_new"),               # new jobs float to the top of their tier
        tier_rank.get((j.get("fit") or {}).get("tier", "unknown"), 3),
        -((j.get("fit") or {}).get("score") or 0),
    ))

    all_jobs = enrich_contacts(all_jobs)

    with open("ranked_jobs.json", "w") as f:
        json.dump(all_jobs, f, indent=2)
    with open("ranked_jobs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["new", "tier", "score", "company", "title", "location", "posted", "url", "reasons"])
        for j in all_jobs:
            fit = j.get("fit") or {}
            w.writerow(["NEW" if j.get("is_new") else "", fit.get("tier", ""),
                        fit.get("score", ""), j["company"], j["title"],
                        j["location"], fmt_date(j.get("date_posted")), j["url"],
                        " | ".join(fit.get("reasons", []))])

    strong = sum(1 for j in all_jobs if (j.get('fit') or {}).get('tier') == 'strong')
    new_n = sum(1 for j in all_jobs if j.get("is_new"))
    print(f"\nDone. {len(all_jobs)} jobs ranked ({strong} strong, {new_n} new) "
          f"-> ranked_jobs.csv / ranked_jobs.json")


if __name__ == "__main__":
    main()
