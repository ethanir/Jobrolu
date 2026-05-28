<div align="center">

# 🗺️ Roadmap & Launch Plan

**Where the project stands, what's left, and how it ships.**

![v1](https://img.shields.io/badge/v1%20engine-complete-brightgreen) ![v2](https://img.shields.io/badge/v2%20coverage-next-orange) ![v3](https://img.shields.io/badge/v3%20launch-planned-blue)

</div>

---

## TL;DR

The engine is **done**. It sources, ranks, drafts outreach, and runs cheap. It found real strong-fit roles and was used to apply to them.

The **only** meaningful work left is **coverage**: the ranking is smart, but it can only rank jobs that make it into the funnel, and right now the funnel sees ~400 companies on 6 ATS types. Widen that, then ship it as a named product with a domain and a landing page.

That's the whole remaining plan. Everything else is polish.

---

## The two-stage funnel (why coverage is the lever)

```
   ALL pulled roles  ~40,000
          |
          |   stage 1 - FREE heuristic score (fast, crude keyword/title match)
          v
   top N by free score  (default 100)
          |
          |   stage 2 - LLM fit-rank (smart, reads the role, the only paid step)
          v
   Strong / Possible / Skip  + reasons + draft email
```

- **Stage 1 is cheap but dumb.** It can underrate a genuinely great role (odd title wording, etc.).
- **Stage 2 is smart but only sees what stage 1 forwards.** A hidden gem ranked #150 by the free scorer never reaches the LLM.
- `TOP_N` controls how deep the smart ranker digs. Higher = catches more buried matches, costs a little more. **It does not change what's at the top** - output is always best-first.

**Implication:** the quality ceiling is set by *what enters the funnel at all*. That's company/ATS coverage. Hence v2.

---

## v1 - the engine ✅ (complete)

| Done | Capability |
|---|---|
| ✅ | Multi-ATS sourcing - Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable (parallel, ~40k roles in <1 min) |
| ✅ | Self-growing company registry (~400 companies) |
| ✅ | Free prefilter + cost-correct LLM funnel (~$1 first run, near-free after) |
| ✅ | Seen-job cache - re-runs only pay for genuinely new jobs |
| ✅ | Honest fit ranking - Strong / Possible / Skip, with reasons and gaps |
| ✅ | AI outreach drafts - subject + body, led by best project, em-dash-free |
| ✅ | Recruiter workflow - LinkedIn discovery + live name personalization |
| ✅ | Standalone viewer - Why-you-fit / Worth-knowing split, per-part copy buttons |
| ✅ | Scan-any-role - paste a JD/URL for a one-off fit-rank + draft |
| ✅ | Hosted live feed - `server.py` + `app.html`: a Refresh button re-runs the pipeline in the background with a live progress bar; new roles append and are flagged, old roles persist. (This is the foundation the v3 hosted version builds on.) |
| ✅ | Smart free pre-filter - sharp heuristic (exact-title, new-grad, SWE family, seniority penalty, location, recency, skill saturation) so the top-N forwarded to the LLM is quality, not look-alikes. |
| ✅ | Bring-your-own-AI ranking - `export_rank.py` + `import_rank.py` rank your top batch for $0 using the free web Claude/ChatGPT. |

**Honest note on Apollo:** contact lookup is wired in, but Apollo's people-search API is gated behind their paid Organization tier (confirmed: free/trial keys return `API_INACCESSIBLE`). The tool detects this and falls back to the free LinkedIn flow automatically. **Do not pay for Apollo for this** - the volume need (a few recruiters) doesn't justify it; LinkedIn covers it free.

---

## v2 - coverage 🟧 (the only real work left)

In strict order of impact-per-effort:

### 1. Expand the company registry - *biggest win, least work* 🛠️ tooling built
Go from ~400 to several thousand companies by seeding the registry from large public sources:
- YC company directory
- Public Greenhouse / Lever / Ashby board listings
- levels.fyi company list

**`seed.py` and `bulk_seed.py` are built and do exactly this:** `bulk_seed.py` ships a
large curated list (175+ known tech employers across Greenhouse/Lever/Ashby) and
validates each against its live board, adding only the ones that actually return jobs.
`seed.py` does the same for a custom file. **$0 API cost**, parallel, fast. Run
`python3 bulk_seed.py` to add the curated set. Remaining work here is just *feeding
even bigger public lists*.

**Honest coverage limits (what we can and can't cleanly scan):**
- ✅ **Greenhouse / Lever / Ashby / SmartRecruiters / Recruitee / Workable** - clean public
  JSON, fully supported, expandable via seeding. This is where the easy wins are.
- ✅ **Workday** - now supported via the CXS public endpoint (`from_workday`, seed with
  `seed_workday.py`). Per-company tenant/site/server, no auth. Dead boards skip quietly.
- ✅ **Adzuna aggregator** - keyword index across many boards (`from_adzuna`), free API
  key, deduped against ATS pulls. Whole new source type beyond company-by-company.
- ⚠️ **iCIMS / Taleo** - large employers, but no clean public API (anti-bot). Still hard.
- ❌ **LinkedIn / Indeed** - actively block scraping and forbid it in their ToS (account-ban
  risk). Deliberately out of scope; the right way to use those is applying directly, by hand.

**Why first:** every company added is more real roles entering the funnel, with zero new code paths - it reuses the existing connectors. This alone meaningfully widens the net.
**Interview value:** "scans N thousand companies across 7 ATS platforms + an aggregator" is a far stronger line than "~400."

### 2. Workday connector - ✅ SHIPPED
`from_workday` hits the public Workday CXS endpoint (`/wday/cxs/{tenant}/{site}/jobs`,
POST, offset pagination, no auth). Tokens are `tenant/site/wdN`. Seed a curated set with
`python3 seed_workday.py`, or add any company from its careers URL. This opens up the
large-enterprise employers that were previously invisible.

### 3. Job-aggregator feed - ✅ SHIPPED (Adzuna)
`from_adzuna` queries Adzuna's keyword index across many boards at once, then dedupes
(by company+title and by url) against the ATS pulls. Needs free `ADZUNA_APP_ID` /
`ADZUNA_APP_KEY` env vars; runs only when they're set, so the pipeline works without it.

> **Scope honesty:** v1 already produces results. v2 is genuine improvement *and* a strong portfolio story, but it is optimization of a channel that already works. It is not a prerequisite for job-searching - direct LinkedIn/Handshake applications and referrals (e.g. the Amazon referral) remain higher-yield channels and should run in parallel, not be replaced by this tool.

---

## v3 - launch 🔵 (after coverage)

| Step | What | Notes |
|---|---|---|
| 4 | **Name + domain** | ✅ Done. **Jobrolu**, live at jobrolu.com. |
| 5 | **Landing page** | ✅ Done. `landing.html`, dark single-screen, served at `/`. |
| 6 | **Deploy the hosted app** | The hosted app (`server.py` + `app.html` + `start.html`, with live refresh and onboarding) works locally. Remaining: deploy this server so it's reachable on the web, plus accounts/multi-user and the API-cost model for real users. |

### Naming: settled
The product is **Jobrolu** (jobrolu.com). Earlier shortlist candidates (Shortlist, Rolescout, Fitscore, Rolu) are kept here only as history.

> **Product-vs-portfolio honesty:** as a hosted *business*, unit economics are tough - third-party data (contacts) is expensive, the market is crowded (Simplify, Jobright, Teal), and the target user (new grads) is price-sensitive. Its highest current value is as a **portfolio project and interview story**: a real, cost-engineered, multi-stage AI system that was actually used to land applications. Launch it because it's a great showcase, not because it's a sure revenue play.

---

## Definition of done

- [~] Registry expanded to several thousand companies (v2.1) - `seed.py` built; feed it bigger lists
- [x] Workday connector live (v2.2)
- [ ] Aggregator feed integrated + deduped (v2.3)
- [ ] Final name chosen
- [ ] Domain purchased
- [ ] Landing page live
- [ ] Hosted viewer with accounts

---

<div align="center">

**Engine: done. Next: widen the net. Then: name it, buy it, ship it.**

MIT © Ethan Irimiciuc

</div>
