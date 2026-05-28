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


def _linkedin_recruiter_search(company):
    """A one-click LinkedIn search for a recruiter at this company — no Apollo needed."""
    import urllib.parse
    q = urllib.parse.quote(f"{company} recruiter")
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def enrich_jobs(jobs, profile, cache, limit=15):
    """For strong-tier jobs: find contacts (if Apollo set) + draft a tailored email.
    Drafts are cached by job id so re-runs don't re-pay. Every job also gets a
    one-click LinkedIn recruiter-search link so the user never has to think."""
    for j in jobs:
        j.setdefault("contacts", [])
        j["linkedin_search"] = _linkedin_recruiter_search(j.get("company", ""))

    strong = [j for j in jobs if (j.get("fit") or {}).get("tier") == "strong"][:limit]
    if not strong:
        return jobs

    have_apollo = bool(os.environ.get("APOLLO_API_KEY"))
    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
    profile_json = json.dumps(profile, indent=2)
    drafts = cache.setdefault("drafts", {})

    print(f"  preparing {len(strong)} strong-fit jobs (contacts + draft emails)...")
    for j in strong:
        if have_apollo:
            j["contacts"] = enrich.find_contacts(j)
        # draft email — cached by job id so we only pay once per job
        jid = j.get("_id", "")
        if jid in drafts:
            j["draft"] = drafts[jid]
        elif client:
            contact = (j["contacts"][0] if j.get("contacts")
                       else {"name": "there", "title": "the hiring team"})
            try:
                j["draft"] = enrich.draft_email(client, profile_json, j, contact)
                drafts[jid] = j["draft"]
            except Exception as e:
                print(f"    draft failed for {j.get('company')}: {e}")
                j["draft"] = ""
        else:
            j["draft"] = ""
    return jobs


def fmt_date(ts):
    return datetime.date.fromtimestamp(ts).isoformat() if ts else ""


def run(profile_path="profile.example.json", progress=None, top_n=None):
    """Run the full pipeline. Importable (used by server.py refresh) and CLI.

    progress: optional callback(stage:str, pct:int, detail:str) for live UI updates.
    top_n:    override TOP_N (how many jobs the LLM ranks); defaults to env/100.
    Returns a summary dict. Writes ranked_jobs.json / .csv as a side effect.
    The cache preserves previously-seen jobs, so re-runs APPEND new postings
    (flagged is_new) without dropping the old ones.
    """
    tn = TOP_N if top_n is None else int(top_n)

    def emit(stage, pct, detail=""):
        if progress:
            try:
                progress(stage, pct, detail)
            except Exception:
                pass
        line = f"  {detail}" if detail else ""
        print(f"[{pct:3d}%] {stage}{(' — ' + detail) if detail else ''}")

    profile = load_profile(profile_path)
    emit("Loading profile", 2, profile.get("name", ""))

    reg = registry.load()
    companies = {f"{c['ats']}:{c['token']}": c for c in SEED_COMPANIES}
    companies.update(reg)
    company_list = list(companies.values())
    emit("Pulling jobs", 8, f"{len(company_list)} companies")
    jobs = sources.pull_all(company_list, include_repo=True)
    emit("Pulling jobs", 45, f"{len(jobs)} roles pulled")

    reg, n_new = registry.discover_from_jobs(jobs, reg)
    registry.save(reg)
    emit("Updating registry", 50, f"+{n_new} new companies ({len(reg)} known)")

    jobs = prefilter.prefilter(jobs, profile)
    emit("Prefiltering", 58, f"{len(jobs)} survived")

    jobs = score.rank_free(jobs, profile)
    emit("Free scoring", 66, f"{len(jobs)} scored")

    cache = jobcache.load()
    n_new_postings = 0
    for j in jobs:
        j["_id"] = jobcache.job_id(j)
        j["is_new"] = j["_id"] not in cache["seen"]
        if j["is_new"]:
            n_new_postings += 1
    emit("Checking for new postings", 70, f"{n_new_postings} brand-new")

    top = jobs[:tn]
    rest = jobs[tn:]

    if tn > 0:
        need_llm = [j for j in top if j["_id"] not in cache["ranked"]]
        cached = [j for j in top if j["_id"] in cache["ranked"]]
        if need_llm:
            import hydrate
            filled = hydrate.hydrate(need_llm)
            emit("Fetching full descriptions", 72,
                 f"{filled} hydrated of {len(need_llm)}")
        emit("Ranking fit (AI)", 74, f"{len(need_llm)} new, {len(cached)} cached")
        if need_llm:
            rank.run(need_llm, profile)               # the only paid step
            for j in need_llm:
                cache["ranked"][j["_id"]] = j.get("fit") or score.heuristic_fit(j)
        for j in cached:
            j["fit"] = cache["ranked"][j["_id"]]
    else:
        emit("Ranking fit (free)", 74, "TOP_N=0, no LLM")
        for j in top:
            j["fit"] = score.heuristic_fit(j)

    for j in rest:                        # everything else: free heuristic tier
        j["fit"] = score.heuristic_fit(j)

    for j in jobs:
        cache["seen"].setdefault(j["_id"], jobcache.today())

    all_jobs = top + rest
    tier_rank = {"strong": 0, "possible": 1, "skip": 2, "unknown": 3}
    all_jobs.sort(key=lambda j: (
        not j.get("is_new"),               # new jobs float to the top of their tier
        tier_rank.get((j.get("fit") or {}).get("tier", "unknown"), 3),
        -((j.get("fit") or {}).get("score") or 0),
    ))

    emit("Finding recruiters + drafting", 88, "strong matches")
    all_jobs = enrich_jobs(all_jobs, profile, cache)
    jobcache.save(cache)                   # save AFTER drafts are cached too

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
    emit("Done", 100, f"{len(all_jobs)} ranked, {strong} strong, {new_n} new")
    return {"total": len(all_jobs), "strong": strong, "new": new_n,
            "new_postings": n_new_postings}


def main():
    profile_path = sys.argv[1] if len(sys.argv) > 1 else "profile.example.json"
    profile = load_profile(profile_path)
    print(f"Profile: {profile.get('name')} | targets: {', '.join(profile.get('target_titles', []))}\n")
    run(profile_path)


if __name__ == "__main__":
    main()
