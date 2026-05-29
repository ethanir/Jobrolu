"""
Orchestrator - the FUNNEL version (cost-correct).

  profile -> pull jobs -> discover companies -> prefilter (free)
          -> heuristic score ALL (free) -> LLM-rank only NEW top-N (cheap)
          -> reuse cached rankings for everything already scanned (free)
          -> heuristic-tier the rest (free) -> ranked feed

Cost: a full run is ~$1 instead of ~$70, because we only pay the LLM for the
top TOP_N most promising jobs. The cache then makes every later run nearly free,
because a job we already paid to rank is never re-ranked. Set TOP_N=0 for a
totally free run (no LLM).

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
    """A one-click LinkedIn search for a recruiter at this company, no Apollo needed."""
    import urllib.parse
    q = urllib.parse.quote(f"{company} recruiter")
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def enrich_jobs(jobs, profile, cache, limit=15, draft=True):
    """For strong-tier jobs: find contacts (if Apollo set) + draft a tailored email.
    Drafts are cached by job id so re-runs don't re-pay. Every job also gets a
    one-click LinkedIn recruiter-search link so the user never has to think.
    draft=False keeps the free recruiter links but skips the paid email drafting,
    so the scheduled background scan stays $0."""
    for j in jobs:
        j.setdefault("contacts", [])
        j["linkedin_search"] = _linkedin_recruiter_search(j.get("company", ""))

    if not draft:
        return jobs

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
        # draft email, cached by job id so we only pay once per job
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


def run(profile_path="profile.example.json", progress=None, top_n=None, draft=True):
    """Run the full pipeline. Importable (used by server.py refresh) and CLI.

    progress: optional callback(stage:str, pct:int, detail:str) for live UI updates.
    top_n:    override TOP_N (how many jobs the LLM ranks); defaults to env/100.
    draft:    when False, skip the paid email drafting (the scheduled scan uses
              this with top_n=0 to pull and score new jobs for $0).
    Returns a summary dict. Writes ranked_jobs.json / .csv as a side effect.
    The cache preserves previously-seen jobs, so re-runs APPEND new postings
    (flagged is_new) without dropping the old ones, and never re-pay for a job
    that was already LLM-ranked.
    """
    tn = TOP_N if top_n is None else int(top_n)

    def emit(stage, pct, detail=""):
        if progress:
            try:
                progress(stage, pct, detail)
            except Exception:
                pass
        line = f"  {detail}" if detail else ""
        print(f"[{pct:3d}%] {stage}{(' - ' + detail) if detail else ''}")

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

    # Build the SHARED pool at full breadth: every software / CS / tech role, all
    # seniorities, all locations, decoupled from any one profile. Per-user scoring
    # narrows each person's feed afterward, so one pool serves a new grad, a senior,
    # a data scientist, a security engineer, and so on. (The owner's own ranking
    # below still scores and LLM-ranks only the owner's top matches, so cost is
    # unchanged.)
    jobs = prefilter.prefilter_generic(jobs)
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

    # Apply every cached LLM ranking up front, to ALL jobs (not just this run's
    # top-N). Two wins: (1) a job we already paid to rank is never re-ranked, so
    # credits compound run over run; (2) a strong match never silently downgrades
    # to a free heuristic tier just because it slipped out of the top-N on a
    # bigger pull. This is what makes "it always saves and builds on" true.
    n_cached_hits = 0
    for j in jobs:
        cached_fit = cache["ranked"].get(j["_id"])
        if cached_fit:
            j["fit"] = cached_fit
            n_cached_hits += 1
    emit("Loading cached rankings", 72, f"{n_cached_hits} already scanned (free)")

    top = jobs[:tn]

    if tn > 0:
        # Pay the LLM ONLY for top-N jobs that have never been ranked before.
        need_llm = [j for j in top if j["_id"] not in cache["ranked"]]
        if need_llm:
            import hydrate
            filled = hydrate.hydrate(need_llm)
            emit("Fetching full descriptions", 74,
                 f"{filled} hydrated of {len(need_llm)}")
            emit("Ranking fit (AI)", 76, f"{len(need_llm)} new to rank, the only paid step")
            rank.run(need_llm, profile)               # the only paid step
            for j in need_llm:
                fit = j.get("fit")
                # only store a real ranking, and remember it forever
                if fit and fit.get("tier") in ("strong", "possible", "skip"):
                    cache["ranked"][j["_id"]] = fit
        else:
            emit("Ranking fit (AI)", 76, "nothing new to pay for, all cached")
    else:
        emit("Ranking fit (free)", 76, "TOP_N=0, no LLM")

    # Anything still without a fit (never LLM-ranked) gets a free heuristic tier.
    for j in jobs:
        if not j.get("fit"):
            j["fit"] = score.heuristic_fit(j)

    for j in jobs:
        cache["seen"].setdefault(j["_id"], jobcache.today())

    all_jobs = jobs
    tier_rank = {"strong": 0, "possible": 1, "skip": 2, "unknown": 3}
    all_jobs.sort(key=lambda j: (
        not j.get("is_new"),               # new jobs float to the top of their tier
        tier_rank.get((j.get("fit") or {}).get("tier", "unknown"), 3),
        -((j.get("fit") or {}).get("score") or 0),
    ))

    emit("Finding recruiters + drafting", 88, "strong matches")
    all_jobs = enrich_jobs(all_jobs, profile, cache, draft=draft)
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
