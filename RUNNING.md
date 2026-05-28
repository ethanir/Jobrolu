# Running JobMatch

Everything runs locally with Python 3.9+. The only required key is an Anthropic
API key (for ranking + draft emails). Apollo is optional.

## Install (once)
```bash
pip install --user -r requirements.txt
```

## The everyday loop
```bash
cd jobmatch
export ANTHROPIC_API_KEY=sk-...        # required for ranking + draft emails
export APOLLO_API_KEY=...              # optional: recruiter contact lookup

python3 main.py my_profile.json        # pull -> funnel -> rank -> cache -> draft
python3 make_ui.py                     # build viewer.html
open viewer.html                       # look at your ranked feed
```
The key resets when you open a new terminal, so re-`export` it each session
(or add it to `~/.zshrc` to persist).

## 1. Build your profile from a resume
```bash
python3 onboard.py my_resume.pdf my_profile.json    # pdf, docx, txt, or md
```
Then open `my_profile.json` and fill anything the resume didn't state — especially
`target_titles`, `work_authorization`, `requires_sponsorship`, and `preferences`
(locations / remote). Better profile = sharper ranking.

## 2. Rank — and the cost model
```bash
python3 main.py my_profile.json
```
What happens:
- Pulls ~40k roles from 401+ companies (parallel, under a minute).
- Free heuristic scores ALL of them ($0).
- Only the top `TOP_N` (default 100) go to the LLM — the only paid step.
- The **cache** means re-runs only pay for genuinely NEW jobs.

Cost: first run ~$1. Later runs usually a few cents (only new jobs are ranked).

Knobs:
- `TOP_N=0 python3 main.py my_profile.json`   — fully free, no LLM at all
- `TOP_N=200 python3 main.py my_profile.json`  — rank more deeply (costs more)

Outputs: `ranked_jobs.csv` (open in Numbers/Excel) and `ranked_jobs.json`.

## 3. View the feed
```bash
python3 make_ui.py        # reads ranked_jobs.json -> viewer.html
open viewer.html
```
Standalone HTML — no server, no npm. Filter by Strong / Possible / Skip. New
postings show a **NEW** badge. Strong matches include a copy-ready outreach email
and either the recruiter's email (if Apollo returned one) or a one-click
"Find recruiter on LinkedIn" link.

Re-run `make_ui.py` only after a fresh `main.py` run. To just look again at the
current feed, `open viewer.html` is enough.

## 4. Or: the hosted site (landing → onboarding → live app)
```bash
uvicorn server:app --port 8000     # or: python3 -m uvicorn server:app --port 8000
# then open http://localhost:8000 in your browser
```
The hosted site is a dark, single-screen experience:
- `/` — **landing page** (`landing.html`): one screen, no scroll, explains the product.
- `/start` — **onboarding** (`start.html`): build your profile two ways —
  **upload a resume** (POSTs to `/api/onboard`, parsed by `onboard.py`) or
  **bring your own AI** (copy the prompt into ChatGPT/Claude, paste the JSON back,
  saved via `/api/profile`). Either way it lands you in the app.
- `/app` — **live feed** (`app.html`): the ranked feed with a **Refresh jobs** button
  that re-runs the whole pipeline in the background while a progress bar tracks each
flagged **NEW** and float to the top; previously-found roles stay put. Paste a
recruiter's name to personalize the email; subject and body each have their own
copy button. This is the version v3 deploys to the web.

Set which profile it uses with `PROFILE_PATH` (defaults to `my_profile.json`):
```bash
PROFILE_PATH=my_profile.json uvicorn server:app --port 8000
```

## 5. Scan one role you found yourself
```bash
python3 scan.py my_profile.json "https://link-to-a-job"     # or paste the JD text
```
Runs the full rank + draft on a single role from anywhere (LinkedIn, Handshake…).

## Contacts (Apollo) — honest note
Finding a recruiter's real email needs an Apollo key AND a paid Apollo plan;
the free plan blocks the people-search API (you'll see a 403, handled gracefully).
Without it, the LinkedIn fallback link does the same job for free. The draft email
always generates regardless — it only needs your Anthropic key.

## What the tiers mean (important)
- **Strong** = the AI read the full posting and confirmed a strong fit. Trustworthy.
- **Possible** = passed the free keyword pre-filter but the AI hasn't verified it yet
  (it wasn't in this run's TOP_N). To get these AI-verified, raise `TOP_N` and re-run.
- **Skip** = clear non-fit (seniority, clearance, wrong role, etc.).

Because only the AI can award "strong", a job showing "Keyword pre-match" in its
reasons has NOT been AI-verified. Before the AI ranks a job it now fetches the full
description (`hydrate.py`), so its decision is based on the whole posting -- including
disqualifiers like "requires security clearance" or "5+ years experience" that a
keyword scan would miss.

## Rank deeper for a big registry
With a large registry (e.g. 475 companies / ~50k jobs), the default `TOP_N=100` can
be too shallow and crowd out your real matches. Bump it:
```bash
TOP_N=300 python3 main.py my_profile.json   # ~$2-3 fresh, ~$0 on cached re-runs
```

## Rank for $0 with your own web AI (bring-your-own-AI)
No API key, no cost. The smart scorer shrinks ~50k jobs to a top batch, you let the
free web Claude/ChatGPT rank that batch, then merge it back:
```bash
python3 export_rank.py 40        # writes rank_me.txt (top 40)
# open Claude.ai or ChatGPT, paste all of rank_me.txt, send
# copy the JSON array it returns into a file: ai_response.json
python3 import_rank.py           # merges rankings back, $0
python3 make_ui.py && open viewer.html
```
This works because the free heuristic pre-filter forwards only the best few dozen
jobs, which is small enough for a chat window to rank well. It does not rank all
50k for free (a chat can't take that much), but it removes the paid step for your
top batch.

## Add Workday employers (large enterprises)
Workday hosts most big companies and was previously invisible. Add a curated set:
```bash
python3 seed_workday.py          # adds ~30 verified Workday employers
python3 seed_workday.py --list   # see what's built in
```
To add your own, open a company's Workday careers page. The URL
`https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` becomes the token
`nvidia/NVIDIAExternalCareerSite/wd5` (tenant / site / server). Add a line in
`seed_workday.py` and re-run. Dead boards are skipped automatically at pull time.

## Enable the Adzuna aggregator (optional, free)
Adzuna searches many job boards at once by keyword. Get a free key at
developer.adzuna.com, then add to `~/.zshrc`:
```bash
export ADZUNA_APP_ID="your_app_id"
export ADZUNA_APP_KEY="your_app_key"
```
Open a new terminal (or `source ~/.zshrc`). The next run pulls Adzuna results too,
deduped against the ATS jobs. Without the keys, it's simply skipped.

## Widen coverage (more companies, $0 API)
```bash
python3 bulk_seed.py --dry-run  # validate the big built-in list, preview only
python3 bulk_seed.py            # add all the live ones (175+ curated companies)
python3 seed.py companies.txt   # or add your own (one per line: "ats token name")
```
Both scripts check each candidate against its live ATS board and add only the ones
that actually return jobs, so the registry never fills with dead tokens. They make
plain HTTP calls (no LLM), so they cost nothing in API and run in parallel. After
seeding, the next `main.py` run automatically pulls from every added company.
File format (lines starting with `#` are ignored):
```
greenhouse  stripe     Stripe
lever       netflix
ashby       ramp        Ramp
```

## Optional pieces
- **Scheduled refresh:** `python3 worker.py --once` (cron) or `--interval 60` (loop).
- **Postgres persistence + dead-listing detection:** set `DATABASE_URL`, then
  `python3 -c "import db; c=db.connect(); db.init_db(c)"`. (Code present; not part
  of the default local flow.)

## Data flow
```
onboard.py -> my_profile.json
                    |
main.py:  sources -> registry -> prefilter -> score (free) -> rank (LLM, cached) -> enrich
   ^                |
   |        ranked_jobs.json / .csv  +  ranked_cache.json
   |                |
   |        make_ui.py -> viewer.html            (standalone, no server)
   |                |
   |        server.py -> app.html                (hosted live app)
   |                       GET  /api/jobs         feed
   |                       GET  /api/jobs/{id}    one role
   |                       POST /api/scan         paste-a-JD
   |                       POST /api/refresh      re-run pipeline in background
   |                       GET  /api/refresh/status   live progress for the bar
   |                            |
   +----------------------------+   "Refresh jobs" button calls main.run() again;
        cache preserves old jobs, appends + flags new ones.
```
