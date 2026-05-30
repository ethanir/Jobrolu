<div align="center">

# 🗺️ Roadmap & Launch Plan

**Where the project stands, what's left, and how it ships.**

![v1](https://img.shields.io/badge/v1%20engine-complete-brightgreen) ![v2](https://img.shields.io/badge/v2%20coverage-ongoing-orange) ![v3](https://img.shields.io/badge/v3%20launch-shipped-brightgreen) ![accounts](https://img.shields.io/badge/accounts%20%2B%20sign--in-live-brightgreen) ![multi-field](https://img.shields.io/badge/v4%20multi--field-live-brightgreen)

</div>

---

## TL;DR

The engine is **done**. It sources, ranks, drafts outreach, and runs cheap. It found real strong-fit roles and was used to apply to them.

The biggest ongoing lever is **coverage**: the ranking is smart, but it can only rank jobs that make it into the funnel, and the funnel has since grown from ~400 companies on 6 ATS types to ~7,265 companies across 9 job systems. The product shipped as Jobrolu (jobrolu.com); widening coverage continues.

That's the whole remaining plan. Everything else is polish.

---

## Now: post-launch focus

Launched and in daily use. Recent and current work, newest first:

- **Mobile UX (in progress, top priority).** The desktop feed is solid; the phone layout needs a real pass. The detail panel was unreachable on tap (first fix shipped); the header and general polish come next. Most job seekers are on phones, so this is the highest-value fix right now.
- **Two-phase feed (shipped).** First load scores your field-relevant matches in seconds, then fills the rest of the pool from a background thread, so a new account sees matches in about 30 seconds instead of minutes.
- **Cross-field matching (validated).** A dozen real resumes across fields confirm each field's own roles rise while off-field roles sink. The engine is field-agnostic.
- **Coverage strategy (researched, decided): free only.** Indeed and LinkedIn have closed their job-search APIs; paid aggregators run roughly $800 a month and mostly resell thin, duplicate listings. The clean ATS platforms we already crawl (Greenhouse alone has 7,500-plus companies) still have thousands more boards to add for $0 with full descriptions. Expand those; do not pay for data.
- **Competitive position (honest).** Rivals like Jobright and Sorce carry 5 to 8 million listings; we do not, and we never claim to win on count. We win on judgment: every role read in full, ranked to fit, the person applies themselves, every field, fit verifiable free with their own AI. That is the opposite of the auto-apply spray the market is turning against.
- **Shelved on purpose.** A paste-in or browser-extension job analyzer (low signal from search-page pastes, plus legal risk in re-posting listings). Build only if real users ask.

The real lever remains users, not more features. One real user's feedback beats another build.

---

## The two-stage funnel (why coverage is the lever)

```
   ALL pulled roles  ~126,000
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
| ✅ | Multi-ATS sourcing - Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Workable, Workday, plus USAJOBS (parallel; ~90k roles live) |
| ✅ | Self-growing company registry (~5,266 companies) |
| ✅ | Free prefilter + cost-correct LLM funnel (~$1 first run, near-free after) |
| ✅ | Seen-job cache - re-runs only pay for genuinely new jobs |
| ✅ | Honest fit ranking - Strong / Possible / Skip, with reasons and gaps |
| ✅ | AI outreach drafts - subject + body, led by best project, em-dash-free |
| ✅ | Recruiter workflow - LinkedIn discovery + live name personalization |
| ✅ | Standalone viewer - Why-you-fit / Worth-knowing split, per-part copy buttons |
| ✅ | Scan-any-role - paste a JD/URL for a one-off fit-rank + draft |
| ✅ | Hosted live feed - `server.py` + `app.html`: a Refresh button re-runs the pipeline in the background with a live progress bar; new roles append and are flagged, old roles persist. (This is the foundation the v3 hosted version builds on.) |
| ✅ | Smart free pre-filter - sharp heuristic (exact-title, new-grad, SWE family, seniority penalty, location, recency, skill saturation) so the top-N forwarded to the LLM is quality, not look-alikes. |
| ✅ | Bring-your-own-AI ranking - `export_rank.py` + `import_rank.py` rank your top batch for $0 using the free web Claude/ChatGPT, and the same flow is built into the app ("Rank with my AI"). |
| ✅ | Accounts + per-user ranked feeds - create an account (email + password, hashed), build a profile (form / resume / your own AI), and the shared pool is scored against your profile for $0 with your own verified rankings overlaid. Sign in / sign out, Profile and Live Feed nav tabs, feed gated behind sign-in. |
| ✅ | Durable pool + fast feed - each Refresh persists the pool to Postgres so redeploys never revert it; the feed caches it in memory and has live search plus clean empty states. |

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

## v3 - launch ✅ (shipped)

| Step | What | Notes |
|---|---|---|
| 4 | **Name + domain** | ✅ Done. **Jobrolu**, live at jobrolu.com. |
| 5 | **Landing page** | ✅ Done. `landing.html`, dark single-screen, served at `/`. |
| 6 | **Deploy the hosted app** | ✅ Done. Live on Railway at www.jobrolu.com, deployed from this repo. Accounts gate the feed; the budget-spending Refresh sits behind a server-side access code, with a spend cap protecting the AI budget. |
| 7 | **Accounts** ✅ live | Real email + password accounts (PBKDF2-hashed, signed session cookies), with **sign in / sign out** and **Profile / Live Feed nav tabs**. There is no owner tier: everyone is an equal account, everyone can use resume upload and bring-your-own-AI ranking, and there is no rate limiting. The feed is gated behind being signed in and having a profile. **Bring-your-own-AI ranking** ("Rank with my AI", with a slider for how many jobs to send) turns your top matches into verified fits using your own ChatGPT/Claude, stored as yours, for $0. The feed has live **search**, clean empty states, and a per-profile scored cache so it loads fast. The shared pool is **durable** (persisted to Postgres). The only budget-spending action, **Refresh**, is visible to everyone but runs only with the access code. |

### Naming: settled
The product is **Jobrolu** (jobrolu.com). Earlier shortlist candidates (Shortlist, Rolescout, Fitscore, Rolu) are kept here only as history.

> **Product-vs-portfolio honesty:** as a hosted *business*, unit economics are tough - third-party data (contacts) is expensive, the market is crowded (Simplify, Jobright, Teal), and the target user (new grads) is price-sensitive. Its highest current value is as a **portfolio project and interview story**: a real, cost-engineered, multi-stage AI system that was actually used to land applications. Launch it because it's a great showcase, not because it's a sure revenue play.

---

## v4 - multi-field expansion ✅ (live, on by default)

The reach lever. The ATS connectors already return *every* role at a company, so the hard part was never sourcing, it was the three tech-specific layers: the prefilter, the profile schema, and the ranking rubric. v4 generalizes all three in a backward-compatible way behind one switch, **`MULTIFIELD`**, which is on by default (set `MULTIFIELD=off` for an instant kill switch, no redeploy needed).

| | What |
|---|---|
| ✅ | **Additive schema** - `field`, `skills_general`, `certifications` added to the profile; the resume parser and bring-your-own-AI prompt fill them for any profession. Tech profiles still use the `skills` dict + `projects`; non-tech ones leave those empty. |
| ✅ | **Field-agnostic scoring** - `score.py` folds `skills_general` + `certifications` into the same whole-word overlap, learns finance / marketing / sales / HR / operations / legal / healthcare / education disciplines for cross-field separation, and gains a gated same-discipline title bonus so a role in the user's own field scores well even when the title words differ. |
| ✅ | **Field-agnostic ranking** - the AI fit rubric judges skills/qualifications overlap (tech stack for technical roles, the field's own skills otherwise) and treats a missing required license or certification as a serious gap. |
| ✅ | **Wider intake** - `prefilter.py` admits professional, resume-driven roles and drops only clearly hourly/manual ones (multi-field mode only); **Adzuna** queries broaden across fields; a new **USAJOBS** connector adds every open US federal job across all occupational series (free key + email, inert without them). |
| ✅ | **Tested + safe** - `test_phase24_multifield.py` covers the schema, the field-agnostic scoring, the discipline classifier, the prefilter gate (off = tech-only, on = professional minus hourly), and the USAJOBS safety default. The full existing suite stays green; tests that assert tech-only behavior pin the switch off. |
| ✅ | **Live** - `MULTIFIELD` defaults on, so multi-field is active in production; `MULTIFIELD=off` reverts instantly with no redeploy. |
| ☐ | **Deepen coverage** - set `USAJOBS_API_KEY` / `USAJOBS_EMAIL`, seed the registry with non-tech companies on the ATSes already supported (Workday covers banks, hospitals, universities, retailers), and raise `BASE_KEEP` as fields grow. |
| ☐ | **Manual-form polish** - pure-manual entry (no resume) still uses tech-shaped skill inputs; add first-class `skills_general` / `certifications` fields to `start.html` for people who type a non-tech profile from scratch. The resume and bring-your-own-AI paths already capture these today. |

**Why it matters:** it turns "the AI fit tool for tech" into "the AI fit tool for any resume-driven professional field" while reusing the entire engine. AI fit-ranking pays off most where roles are resume-driven and fit is ambiguous (tech, finance, marketing, healthcare, consulting), which is exactly the scope chosen, hourly and manual roles are left out on purpose.

---

## Definition of done

- [~] Registry expanded to several thousand companies (v2.1) - `seed.py` built; feed it bigger lists
- [x] Workday connector live (v2.2)
- [x] Aggregator feed integrated + deduped (v2.3, Adzuna)
- [x] Final name chosen (Jobrolu)
- [x] Domain purchased (jobrolu.com)
- [x] Landing page live
- [x] Hosted feed with per-user accounts (sign up, per-user profiles + ranked feeds, in-app bring-your-own-AI ranking)
- [x] Multi-field engine (v4) - live, on by default (`MULTIFIELD=off` reverts); deepen with USAJOBS keys + non-tech company seeding

---

<div align="center">

**Engine: done. Next: widen the net. Then: name it, buy it, ship it.**

MIT © Ethan Irimiciuc

</div>
