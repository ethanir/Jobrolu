<div align="center">

# 🎯 JobMatch

### Stop spraying applications. Find the roles that actually fit — and the human to email.

![status](https://img.shields.io/badge/status-complete%20%E2%80%94%20v1-brightgreen) ![field](https://img.shields.io/badge/field-Software%20Engineering-blue) ![python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white) ![fastapi](https://img.shields.io/badge/FastAPI-optional%20API-009688?logo=fastapi&logoColor=white) ![postgres](https://img.shields.io/badge/PostgreSQL-optional-4169E1?logo=postgresql&logoColor=white) ![license](https://img.shields.io/badge/license-MIT-black)

![sources](https://img.shields.io/badge/sources-6%20ATS%20%2B%20curated-success) ![registry](https://img.shields.io/badge/company%20registry-self--growing-orange) ![ranking](https://img.shields.io/badge/ranking-funnel%20%2B%20LLM-ff5c5c) ![cache](https://img.shields.io/badge/re--runs-near--free-9b59b6) ![auto--apply](https://img.shields.io/badge/auto--apply-never-lightgrey)

**One profile. Thousands of live roles. Ranked honestly. With the recruiter's email attached.**

</div>

---

## ✨ Overview

Most job tools do one of two things: **autofill** the same form a hundred times, or dump a feed of loosely-matched listings on you. Neither tells you *which roles are actually worth your time* or *who to talk to*. That's volume, not strategy.

**JobMatch is different.** It runs the job search the way it actually works:

> **Aggregate** from the cleanest sources → **rank** every role honestly against *your* real profile → **hand you the recruiter** to email, with a personalized draft.

You stay in control of the final send. No spammy auto-apply, no getting your LinkedIn banned, no hallucinated matches. Just the best-fit roles and a clear next move for each.

---

## 🧠 How it works

```
  resume / profile  (onboard.py: PDF/DOCX -> structured profile via LLM)
         |
         v
  +----------------------------------------------+
  |  SOURCING   7 ATS + aggregator, self-growing   |
  |             company registry + curated lists  | --> ~40k live roles, parallel pull
  +----------------------------------------------+
         |
         v   free, rule-based
   PREFILTER  (title - seniority - location)        ~40k -> ~7k
         |
         v   free heuristic scorer (score.py), $0
   FUNNEL     skill overlap + title + location + recency, ranks ALL
         |
         v   fetch full posting for the TOP_N (hydrate.py), $0
   HYDRATE    so the AI sees the whole job page, not a snippet
         |
         v   LLM rubric — TOP_N only (default 100), the only paid step
   FIT-RANK   score - tier - reasons - gaps        ~$1 first run
         |
         v   cache (jobcache.py): only NEW jobs ever hit the LLM
   CACHE      re-runs are near-free; new postings flagged "NEW"
         |
         v   strong matches only
   ENRICH     recruiter contacts (Apollo, optional) + AI outreach draft
         |
         v
   make_ui.py -> viewer.html   filterable feed, fit reasons, copy-ready email
```

---

## 🚀 Features

| | Feature | What it does |
|---|---|---|
| 🔎 | **Multi-source sourcing** | Pulls live roles from **7 ATS platforms** (Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable, Workday) plus the Adzuna keyword aggregator and curated new-grad lists. Pulls run in parallel — ~50k roles in a couple of minutes. |
| 🌱 | **Self-growing registry** | Every job URL teaches it a new company token, so coverage **compounds automatically** — no manual company list to maintain. |
| 📈 | **Registry seeding (`seed.py` / `bulk_seed.py`)** | Widen coverage on demand: validates candidate companies against their live ATS boards and adds only the ones that really return jobs. `bulk_seed.py` ships a large curated list (175+ known tech employers across Greenhouse/Lever/Ashby). **$0 API cost** (plain HTTP, no LLM), runs in parallel. This is the v2 "coverage" lever — more companies in the funnel, same ranking cost. |
| 💸 | **Cost-correct funnel** | A free heuristic scores *every* role; only the top N (default 100) hit the LLM. A full run costs **~$1, not ~$70.** Set `TOP_N=0` for a fully free run. |
| 🧠 | **Honest fit ranking** | The LLM scores your top matches with reasons and gaps. It will tell you to **skip** a bad fit. |
| ♻️ | **Seen-job cache** | Remembers what it already ranked, so **re-runs only pay for genuinely new jobs.** New postings are flagged **NEW** and float to the top. |
| ✍️ | **AI outreach drafts** | Every strong match gets a short, personalized email with its own **subject line**, led by your most relevant project, generated from your full profile. Subject and body each have their own one-click copy button. No em dashes (so it never reads as AI-written). You review and send from your own inbox. |
| 📇 | **Recruiter workflow** | One-click **"Find recruiter on LinkedIn"** for every strong match. Paste the recruiter's name back in and the email greeting + subject personalize instantly. (Apollo contact lookup is wired in but requires Apollo's paid API tier — the free LinkedIn flow is the default and costs nothing.) |
| 🖥️ | **Standalone viewer** | `make_ui.py` bakes the feed into a single `viewer.html` — no server, no build step. Filterable by Strong / Possible / Skip. Each role shows **Why you fit** (the positives) and **Worth knowing** (the honest concerns) split cleanly, plus matched skills and gaps. |
| 🔎 | **Scan any role** | Paste a JD or URL from LinkedIn / Handshake → instant fit-rank + draft for one role you found yourself. |
| 🧠 | **Smart free pre-filter** | A sharp heuristic scorer (exact-title, new-grad signal, SWE role family, seniority penalty, location, recency, skill saturation) ranks all ~50k jobs for **$0** and forwards only the genuinely-best to the LLM, so the paid step sees quality, not look-alikes. |
| ✅ | **"Strong" means AI-verified** | The free keyword pre-filter never awards "strong" — the best it can show is "possible". Only the LLM, after reading the full posting, can mark a job "strong". So every green "Strong fit" is one the AI actually analyzed, not a keyword coincidence. |
| 📄 | **Full-description hydration (`hydrate.py`)** | Before the AI ranks a job, the complete posting is fetched (Greenhouse, Lever, SmartRecruiters, Workday detail endpoints), so the AI sees everything on the page — including disqualifiers like "requires security clearance" or "5+ years". Runs only on the jobs about to be ranked, in parallel, $0. |
| 🎯 | **Whole-word skill matching + disqualifier signals** | The pre-filter matches skills as whole words (so "c" no longer matches "clearance") and down-weights security-clearance / high-experience roles a new grad can't satisfy. |
| 💸 | **Bring-your-own-AI ranking (`export_rank.py` / `import_rank.py`)** | Rank your top jobs for **$0 API** using the free web version of Claude or ChatGPT: export a ready-to-paste file, paste it into the web chat, paste the result back. The smart pre-filter shrinks the set first so a chat window can handle it. |
| 🛑 | **No auto-apply, no auto-send** | Deliberately. It protects your accounts, your sender reputation, and the quality of every application. |
| 🔄 | **Hosted live feed + refresh** | Run `server.py` and open the app in a browser: a single **Refresh jobs** button re-runs the whole pipeline in the background with a **live progress bar**. New postings are appended and flagged **NEW**; previously-found roles never disappear. This is the hosted version that v3 ships. |

---

## 🏗️ Tech stack

**Backend** Python · FastAPI (optional API) · PostgreSQL (optional persistence)
**Intelligence** LLM fit-ranking + outreach drafting (Anthropic, Sonnet)
**Enrichment** Apollo.io (contacts, optional) · Hunter.io (email verification, optional)
**Sources** Greenhouse · Lever · Ashby · SmartRecruiters · Recruitee · Workable
**Frontend** Standalone single-file React viewer (`make_ui.py` → `viewer.html`) — no build step

---

## ⚡ Quickstart

```bash
git clone https://github.com/ethanir/jobmatch.git
cd jobmatch
pip install --user -r requirements.txt

# 1) build your profile from your resume (needs ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-...
python3 onboard.py my_resume.pdf my_profile.json

# 2) rank (funnel + cache keep it cheap: ~$1 first run, near-free after)
export APOLLO_API_KEY=...               # optional: recruiter contacts
python3 main.py my_profile.json         # -> ranked_jobs.csv / ranked_jobs.json

# 3) view the feed (standalone, no server)
python3 make_ui.py                      # -> viewer.html
open viewer.html

# OR run the hosted live version with a refresh button + progress bar
uvicorn server:app --port 8000          # then open http://localhost:8000
```

**Free run** (no LLM cost at all): `TOP_N=0 python3 main.py my_profile.json`
**Rank more deeply** (costs more): `TOP_N=200 python3 main.py my_profile.json`
**Widen coverage** (add more companies, $0 API): `python3 bulk_seed.py` (or `seed.py`)
**Keep it fresh on a schedule:** `python3 worker.py --interval 60`

### Coverage, cost, and speed — how they relate
Three independent things, and only one of them costs money:

- **How many companies you pull from** (`seed.py` grows this). Pulling is plain HTTP and runs in parallel, so a bigger registry means **more jobs in the funnel at $0 extra API cost** and only a little more time (the pull is parallelized). This is the main quality lever.
- **The free heuristic scorer** rates *every* pulled job for $0. It picks which jobs the LLM sees.
- **How deep the LLM ranks** (`TOP_N`) is the **only** paid step, and its cost is fixed by `TOP_N` no matter how big the pool is:

| `TOP_N` | LLM ranks | Fresh-run cost | Re-run cost (cache) |
|---|---|---|---|
| 100 (default) | top 100 | ~$1 | ~$0 |
| 300 (good for a big pool) | top 300 | ~$2-3 | ~$0 |
| 0 + web-AI export | top ~30-40 via free web chat | **$0** | $0 |

For a large registry (e.g. 475 companies / ~50k jobs), `TOP_N=100` is too shallow — your real matches can get crowded out, so bump to `TOP_N=300`. Or go fully free: `TOP_N=0` then use `export_rank.py` + your own web AI. The smart scorer makes either work well because it forwards quality, not noise.

---

## 📁 Project structure

```
jobmatch/
├── main.py                 # orchestrator: source -> filter -> funnel -> rank -> cache -> enrich -> output
├── onboard.py              # resume (PDF/DOCX/TXT) -> structured profile via LLM
├── sources.py              # 7 ATS connectors + Adzuna aggregator + curated lists (parallel)
├── registry.py             # self-growing company->token registry
├── seed.py                 # widen the registry: validate + add companies (v2 coverage, $0 API)
├── bulk_seed.py            # large curated company list, validated + added in bulk ($0 API)
├── seed_workday.py         # add Workday-hosted employers (tenant/site/wdN format)
├── prefilter.py            # free rule-based cut before any LLM call
├── score.py                # smart free heuristic funnel scorer ($0) — picks what the LLM sees
├── hydrate.py              # fetch full job descriptions before AI ranking ($0)
├── export_rank.py          # export top jobs to rank with your free web AI ($0)
├── import_rank.py          # merge web-AI rankings back into the feed ($0)
├── rank.py                 # LLM fit-ranking engine (parallel)
├── jobcache.py             # seen-job + ranking cache — re-runs only pay for new jobs
├── enrich.py               # recruiter contacts (Apollo) + email verify + outreach draft
├── scan.py                 # paste-a-JD/URL -> full pipeline on one role
├── make_ui.py              # bakes ranked_jobs.json -> standalone viewer.html
├── landing.html            # dark single-screen marketing landing page (served at /)
├── start.html              # onboarding: upload resume or bring-your-own-AI (served at /start)
├── app.html                # hosted single-page app: live feed + refresh button + progress bar
├── server.py               # FastAPI: / landing, /start onboarding, /app feed, /api/* endpoints
├── db.py                   # optional Postgres schema + freshness/death-detection
├── worker.py               # optional scheduled re-pull
├── prompts.py              # profile schema + all LLM prompts (the heart)
├── profile.example.json    # sample candidate profile
├── RUNNING.md              # full how-to for every piece
├── ROADMAP.md              # what's left: coverage plan, name shortlist, launch steps
└── BUILD_SPEC.md           # full spec — anyone can rebuild the product from it
```

---

## 🗺️ Roadmap

**v1 — the engine (done).** Everything below works today, runs locally, and is the version that found and applied to real roles.

- [x] Multi-ATS sourcing across 6 platforms (parallel, ~40k roles in under a minute)
- [x] Self-growing company registry
- [x] Free prefilter + cost-correct LLM funnel (~$1 first run, near-free after)
- [x] Seen-job cache — re-runs only pay for genuinely new jobs
- [x] Honest fit ranking — Strong / Possible / Skip with reasons and gaps
- [x] Personalized AI outreach drafts per strong match (subject + body, em-dash-free)
- [x] Recruiter workflow — LinkedIn discovery + live name personalization
- [x] Standalone single-file viewer (Why-you-fit / Worth-knowing split, copy-ready email)
- [x] Scan-any-role (paste a JD/URL)

**v2 — the only thing left: coverage.** The ranking engine is strong; the one real ceiling is *how many jobs enter the funnel*. v1 scanned ~400 companies on 6 ATS types; v2 now covers 7 ATS platforms plus an aggregator. These three steps, in order of impact, take it from "a great slice" toward "scans everywhere," and are the entire remaining build:

1. **Expand the company registry** *(biggest win, least work)* — seed it from large public sources (YC company list, public Greenhouse / Lever / Ashby board directories, levels.fyi company list) to go from ~400 to several thousand companies. More companies in = more real roles ranked. `seed.py` / `bulk_seed.py` do this.
2. **Workday connector — ✅ done.** `from_workday` hits the public Workday CXS endpoint; seed employers with `seed_workday.py`. Opens up large-enterprise employers that were invisible before.
3. **Job-aggregator feed — ✅ done (Adzuna).** `from_adzuna` queries one keyword index across many boards, deduped against ATS pulls. Set `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` to enable.

**v3 — launch.** Once coverage is wide, ship it as a real product:

4. **Pick a final name** (`JobMatch` is taken — see `ROADMAP.md` for the shortlist).
5. **Buy the domain.**
6. **Build the landing page + hosted version** (accounts, the viewer served from the web instead of a local file).

> See **`ROADMAP.md`** for the full plan, name shortlist, and honest notes on scope, cost, and what's worth doing.

---

## 🔒 Principles

- **Honest over hopeful** — a correct "skip" beats an inflated match.
- **Human in the loop** — we draft; you send. Always.
- **Respect the inbox** — verified emails only, sane volume, your own sender identity.
- **Fresh or nothing** — a stale listing is a broken promise.

---

<div align="center">

**Built for people who'd rather send 20 great applications than 200 blind ones.**

MIT © Ethan Irimiciuc

</div>
